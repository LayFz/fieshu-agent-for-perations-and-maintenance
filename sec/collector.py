"""情报采集器：按情报源抓取 → 抽 CVE → 写漏洞台账（vulns）。确定性代码，不过大模型、不花 token。

情报源类型（intel_sources.kind）：
  kev        CISA KEV 目录（近 30 天收录的进台账并标 kev）
  rss        RSS / Atom（厂商安全公告、Ubuntu USN、oss-security、Reddit…）
  jsonfeed   JSON Feed（Kubernetes 官方 CVE feed）
  ghsa       GitHub Advisory 全局库（已审核的）
  ghsa_repo  某个 GitHub 仓库的安全公告（url 填 owner/repo）
  mastodon   Mastodon 话题时间线 API（公开实例，替代推特的舆情源）
  hackernews Hacker News（Algolia 搜索）
  x          X/Twitter 最近搜索 API v2（需要 Bearer Token，读取是付费接口，默认关闭）

category：official（官方/厂商）/ social（舆情）。social 来源提到的 CVE 计入「舆情热度」。
抓到的文本是外部任何人都能写的内容：只做 CVE 抽取和入库，不交给有写/执行权限的应用。
"""
import html
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

from core import store
from . import feeds

UA = "Mozilla/5.0 (compatible; feishu-agent-intel/1.0)"
CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.I)
_TIMEOUT = 30
_MAX_ITEMS = 100
_lock = threading.Lock()

DEFAULT_SOURCES = [
    # 官方 / 漏洞库
    {"name": "CISA KEV（在野利用目录）", "kind": "kev", "category": "official", "url": feeds.KEV_URL, "interval_min": 180},
    {"name": "GitHub Advisory 全局库", "kind": "ghsa", "category": "official", "url": "https://api.github.com/advisories", "interval_min": 60},
    {"name": "Kubernetes 官方 CVE feed", "kind": "jsonfeed", "category": "official",
     "url": "https://kubernetes.io/docs/reference/issues-security/official-cve-feed/index.json", "interval_min": 360},
    {"name": "CISA 安全公告（部分网络被 CDN 403，默认关）", "kind": "rss", "category": "official",
     "url": "https://www.cisa.gov/cybersecurity-advisories/all.xml", "interval_min": 180, "enabled": 0},
    # 厂商（我们栈里的）
    {"name": "GitLab 补丁发布", "kind": "rss", "category": "official", "url": "https://about.gitlab.com/security-releases.xml", "interval_min": 180},
    {"name": "Ubuntu 安全公告 USN", "kind": "rss", "category": "official", "url": "https://ubuntu.com/security/notices/rss.xml", "interval_min": 180},
    {"name": "ArgoCD 安全公告", "kind": "ghsa_repo", "category": "official", "url": "argoproj/argo-cd", "interval_min": 360},
    {"name": "Casdoor 安全公告", "kind": "ghsa_repo", "category": "official", "url": "casdoor/casdoor", "interval_min": 360},
    {"name": "oss-security 邮件列表", "kind": "rss", "category": "official", "url": "https://seclists.org/rss/oss-sec.rss", "interval_min": 120},
    # 舆情（推特替代：公开可读）
    {"name": "Mastodon #cve（mastodon.social）", "kind": "mastodon", "category": "social",
     "url": "https://mastodon.social/api/v1/timelines/tag/cve", "interval_min": 30},
    {"name": "Reddit r/netsec", "kind": "rss", "category": "social", "url": "https://www.reddit.com/r/netsec/new/.rss", "interval_min": 60},
    {"name": "Hacker News · CVE", "kind": "hackernews", "category": "social", "url": "https://hn.algolia.com/api/v1/search_by_date",
     "query": "CVE", "interval_min": 60},
    {"name": "X（推特）· 需 Bearer Token", "kind": "x", "category": "social", "url": "https://api.twitter.com/2/tweets/search/recent",
     "query": "(CVE OR 0day OR \"actively exploited\") -is:retweet lang:en", "interval_min": 60, "enabled": 0},
]


def seed_defaults():
    """情报源表为空时写入默认源（只做一次；之后以后台为准）。"""
    if store.list_intel_sources():
        return 0
    for s in DEFAULT_SOURCES:
        store.create_intel_source(**dict({"enabled": 1}, **s))
    return len(DEFAULT_SOURCES)


def _cves(*texts):
    return sorted({m.upper() for t in texts if t for m in CVE_RE.findall(t)})


def _text(s, n=600):
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    return re.sub(r"\s+", " ", s).strip()[:n]


def _get(url, **kw):
    r = requests.get(url, headers=dict({"User-Agent": UA}, **kw.pop("headers", {})), timeout=_TIMEOUT, **kw)
    r.raise_for_status()
    return r


# ---------- 各类源 → 统一条目 {uid,title,url,summary,published,cves,extra} ----------
def _fetch_kev(src):
    d = feeds._kev()
    since = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    out = []
    for v in d.get("vulnerabilities") or []:
        if (v.get("dateAdded") or "") < since:
            continue
        cve = v.get("cveID")
        out.append({"uid": cve, "title": v.get("vulnerabilityName") or cve,
                    "url": f"https://nvd.nist.gov/vuln/detail/{cve}", "summary": _text(v.get("shortDescription")),
                    "published": v.get("dateAdded"), "cves": [cve],
                    "extra": {"kev": True, "product": f"{v.get('vendorProject')} {v.get('product')}".strip(),
                              "ransomware": v.get("knownRansomwareCampaignUse"), "due": v.get("dueDate")}})
    return out


def _fetch_rss(src):
    root = ET.fromstring(_get(src["url"]).content)
    out = []
    atom = "{http://www.w3.org/2005/Atom}"
    entries = root.findall(".//item") or root.findall(f".//{atom}entry")
    for e in entries[:_MAX_ITEMS]:
        if e.tag == "item":
            title, link = e.findtext("title"), e.findtext("link")
            summary, pub = e.findtext("description"), e.findtext("pubDate")
            uid = e.findtext("guid") or link or title
        else:
            title = e.findtext(f"{atom}title")
            ln = e.find(f"{atom}link")
            link = ln.get("href") if ln is not None else None
            summary = e.findtext(f"{atom}summary") or e.findtext(f"{atom}content")
            pub = e.findtext(f"{atom}published") or e.findtext(f"{atom}updated")
            uid = e.findtext(f"{atom}id") or link or title
        out.append({"uid": uid, "title": _text(title, 300), "url": link, "summary": _text(summary),
                    "published": pub, "cves": _cves(title, summary)})
    return out


def _fetch_jsonfeed(src):
    d = _get(src["url"]).json()
    out = []
    for it in (d.get("items") or [])[:_MAX_ITEMS * 3]:
        t, body = it.get("title") or it.get("id"), it.get("content_text") or it.get("summary")
        out.append({"uid": it.get("id") or it.get("url"), "title": _text(t, 300), "url": it.get("url"),
                    "summary": _text(body), "published": it.get("date_published"), "cves": _cves(it.get("id"), t, body)})
    return out


def _gh_headers(src):
    h = {"Accept": "application/vnd.github+json"}
    if src.get("token"):
        h["Authorization"] = f"Bearer {src['token']}"
    return h


def _ghsa_item(a):
    cve = a.get("cve_id")
    sev = a.get("severity")
    cvss = ((a.get("cvss") or {}).get("score")) or ((a.get("cvss_severities") or {}).get("cvss_v3") or {}).get("score")
    pkgs = [f"{(v.get('package') or {}).get('ecosystem')}:{(v.get('package') or {}).get('name')}"
            for v in a.get("vulnerabilities") or [] if v.get("package")]
    return {"uid": a.get("ghsa_id"), "title": _text(a.get("summary"), 300), "url": a.get("html_url"),
            "summary": _text(a.get("description"), 600), "published": a.get("published_at"),
            "cves": [cve] if cve else _cves(a.get("summary"), a.get("description")),
            "extra": {"severity": sev, "cvss": cvss, "packages": pkgs[:10]}}


def _fetch_ghsa(src):
    r = _get(src["url"], params={"type": "reviewed", "per_page": 100, "sort": "published", "direction": "desc"},
             headers=_gh_headers(src))
    return [_ghsa_item(a) for a in r.json()]


def _fetch_ghsa_repo(src):
    repo = src["url"].strip().strip("/")
    r = _get(f"https://api.github.com/repos/{repo}/security-advisories", params={"per_page": 50},
             headers=_gh_headers(src))
    items = [_ghsa_item(a) for a in r.json()]
    for it in items:
        it["extra"]["packages"] = it["extra"].get("packages") or [repo]
    return items


def _fetch_mastodon(src):
    d = _get(src["url"], params={"limit": 40}).json()
    out = []
    for s in d:
        body = _text(s.get("content"))
        out.append({"uid": s.get("id"), "title": body[:140], "url": s.get("url"), "summary": body,
                    "published": s.get("created_at"), "cves": _cves(body),
                    "extra": {"account": (s.get("account") or {}).get("acct"), "reblogs": s.get("reblogs_count"),
                              "favourites": s.get("favourites_count")}})
    return out


def _fetch_hackernews(src):
    d = _get(src["url"], params={"query": src.get("query") or "CVE", "tags": "story", "hitsPerPage": 50}).json()
    out = []
    for h in d.get("hits") or []:
        t, body = h.get("title"), h.get("story_text")
        out.append({"uid": h.get("objectID"), "title": _text(t, 300),
                    "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                    "summary": _text(body), "published": h.get("created_at"), "cves": _cves(t, body, h.get("url")),
                    "extra": {"points": h.get("points"), "comments": h.get("num_comments")}})
    return out


def _fetch_x(src):
    if not src.get("token"):
        raise RuntimeError("没配 X API Bearer Token（X 读取接口需付费档位；拿不到就先用 Mastodon/Reddit/HN 等公开源）")
    r = _get(src["url"], params={"query": src.get("query") or "CVE", "max_results": 50,
                                 "tweet.fields": "created_at,public_metrics,author_id"},
             headers={"Authorization": f"Bearer {src['token']}"})
    out = []
    for t in r.json().get("data") or []:
        body = t.get("text") or ""
        out.append({"uid": t.get("id"), "title": body[:140], "url": f"https://x.com/i/status/{t.get('id')}",
                    "summary": body, "published": t.get("created_at"), "cves": _cves(body),
                    "extra": {"metrics": t.get("public_metrics")}})
    return out


FETCHERS = {"kev": _fetch_kev, "rss": _fetch_rss, "jsonfeed": _fetch_jsonfeed, "ghsa": _fetch_ghsa,
            "ghsa_repo": _fetch_ghsa_repo, "mastodon": _fetch_mastodon, "hackernews": _fetch_hackernews, "x": _fetch_x}
KINDS = tuple(FETCHERS)


# ---------- 入库 ----------
LEDGER_WINDOW_DAYS = 30     # 只有近 N 天的情报进台账；更早的（厂商发布史、多年累积的 feed）只存情报不进台账


def _parse_date(s):
    if not s:
        return None
    s = str(s).strip()
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _recent(published, days=LEDGER_WINDOW_DAYS):
    d = _parse_date(published)
    return d is None or d >= datetime.now(timezone.utc) - timedelta(days=days)   # 没日期的按新的处理


def ingest(src, items):
    """条目去重入库；近 30 天的新条目里的 CVE 合并进台账。返回 (新条目数, 进台账的 CVE 集合)。"""
    new, touched = 0, set()
    social = src.get("category") == "social"
    for it in items:
        if not it.get("uid"):
            continue
        if store.add_intel_item(src["id"], str(it["uid"])[:300], it.get("title"), it.get("url"), it.get("summary"),
                                it.get("published"), it.get("cves") or [], it.get("extra")) is None:
            continue
        new += 1
        if not _recent(it.get("published")):
            continue
        ex = it.get("extra") or {}
        for cve in it.get("cves") or []:
            touched.add(cve)
            f = {"sources": [src["name"]], "add_mentions": 1, "add_social": 1 if social else 0}
            if not social and len(it.get("cves") or []) == 1 and it.get("title"):
                f["title"] = it["title"]                   # 官方源单 CVE 条目的标题最准
                f["description"] = it.get("summary")
            if ex.get("kev"):
                f.update(kev=True, kev_added=it.get("published"), ransomware=ex.get("ransomware"),
                         products=[ex["product"]] if ex.get("product") else [])
            if ex.get("severity"):
                f["severity"] = ex["severity"]
            if ex.get("cvss"):
                f["cvss"] = ex["cvss"]
            if ex.get("packages"):
                f["products"] = ex["packages"]
            store.upsert_vuln(cve, **f)
    return new, touched


def fetch_source(source_id):
    src = store.get_intel_source(source_id)
    if not src:
        return {"ok": False, "error": "情报源不存在"}
    fn = FETCHERS.get(src["kind"])
    try:
        if not fn:
            raise RuntimeError(f"不支持的类型 {src['kind']}")
        items = fn(src)
        new, touched = ingest(src, items)
        store.set_intel_source_result(src["id"], "ok", "", new)
        return {"ok": True, "fetched": len(items), "new": new, "cves": len(touched)}
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:300]}"
        if src.get("token"):
            msg = msg.replace(src["token"], "***")
        store.set_intel_source_result(src["id"], "error", msg, 0)
        return {"ok": False, "error": msg}


def enrich_epss(limit=100):
    cves = store.vulns_needing_epss(limit)
    if not cves:
        return 0
    try:
        scores = feeds.epss(cves)
    except Exception:
        return 0
    now = time.time()
    for c in cves:
        s = scores.get(c)
        store.upsert_vuln(c, epss=s["epss"] if s else None, epss_pct=s["percentile"] if s else None, epss_at=now)
    return len(cves)


def sync_due(now=None):
    """调度线程调：到期的启用情报源逐个抓取，再补 EPSS。已有一轮在跑就跳过。"""
    now = now or time.time()
    if not _lock.acquire(blocking=False):
        return []
    try:
        done = []
        for src in store.list_intel_sources():
            if not src["enabled"]:
                continue
            if src.get("last_fetch") and now - src["last_fetch"] < max(10, int(src.get("interval_min") or 60)) * 60:
                continue
            rep = fetch_source(src["id"])
            done.append((src["id"], rep))
            print(f"[情报] {src['name']} → " + (f"新增 {rep['new']} 条、涉及 {rep['cves']} 个 CVE" if rep["ok"]
                                                 else f"失败：{rep['error'][:120]}"), flush=True)
        if done:
            enrich_epss()
        return done
    finally:
        _lock.release()
