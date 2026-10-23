"""CVE 情报源（只读）：CISA KEV（在野利用目录）、OSV（按包+版本查受影响漏洞，含 GHSA/Go/PyPI/npm…）、
FIRST EPSS（被利用概率）。

返回的漏洞描述是外部任何人都能写的文本：这些工具在元数据里标 external，P6 污染 guard 上线前，
只给只读应用勾选（别和 ssh_exec / kubectl 变更 / 提 MR 放在同一个应用里）。
"""
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
OSV_QUERY = "https://api.osv.dev/v1/query"
OSV_VULN = "https://api.osv.dev/v1/vulns/{}"
EPSS_URL = "https://api.first.org/data/v1/epss"
_TIMEOUT = 30
_KEV_TTL = 3600

_kev_cache = {"ts": 0, "data": None}
_kev_lock = threading.Lock()


def _kev():
    with _kev_lock:
        if _kev_cache["data"] is None or time.time() - _kev_cache["ts"] > _KEV_TTL:
            r = requests.get(KEV_URL, timeout=_TIMEOUT)
            r.raise_for_status()
            _kev_cache.update(ts=time.time(), data=r.json())
        return _kev_cache["data"]


def kev_recent(days=30, keyword=None, limit=50):
    """KEV 最近 days 天新增的条目（按收录日期倒序），keyword 按厂商/产品/名称模糊过滤。"""
    d = _kev()
    days = max(1, min(int(days or 30), 3650))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    kw = (keyword or "").strip().lower()
    rows = []
    for v in d.get("vulnerabilities") or []:
        if (v.get("dateAdded") or "") < since:
            continue
        text = " ".join(str(v.get(k) or "") for k in ("vendorProject", "product", "vulnerabilityName")).lower()
        if kw and kw not in text:
            continue
        rows.append({"cve": v.get("cveID"), "vendor": v.get("vendorProject"), "product": v.get("product"),
                     "name": v.get("vulnerabilityName"), "added": v.get("dateAdded"), "due": v.get("dueDate"),
                     "ransomware": v.get("knownRansomwareCampaignUse"),
                     "desc": (v.get("shortDescription") or "")[:300]})
    rows.sort(key=lambda x: x["added"] or "", reverse=True)
    limit = max(1, min(int(limit or 50), 200))
    return {"catalog": d.get("catalogVersion"), "total_in_catalog": d.get("count"), "since": since,
            "matched": len(rows), "items": rows[:limit], "truncated": len(rows) > limit}


def in_kev(cve_ids):
    ids = {c.upper() for c in cve_ids if c}
    return {v["cveID"]: v.get("dateAdded") for v in _kev().get("vulnerabilities") or [] if v.get("cveID") in ids}


def _severity(v):
    ds = v.get("database_specific") or {}
    if ds.get("severity"):
        return ds["severity"]
    for s in v.get("severity") or []:
        if s.get("score"):
            return s["score"]
    return None


def _fixed(v, package):
    out = []
    for a in v.get("affected") or []:
        if package and (a.get("package") or {}).get("name") != package:
            continue
        for rg in a.get("ranges") or []:
            for e in rg.get("events") or []:
                if e.get("fixed"):
                    out.append(e["fixed"])
    return sorted(set(out))


def osv_query(ecosystem, package, version):
    """某个包的某个版本受哪些漏洞影响。ecosystem 如 Go / PyPI / npm / Maven / crates.io / Debian:12 / Alpine:v3.20。"""
    if not (ecosystem and package and version):
        return {"error": "ecosystem、package、version 都要填"}
    r = requests.post(OSV_QUERY, json={"package": {"name": package, "ecosystem": ecosystem}, "version": version},
                      timeout=_TIMEOUT)
    r.raise_for_status()
    vulns = r.json().get("vulns") or []
    items = []
    for v in vulns:
        cves = [a for a in (v.get("aliases") or []) if a.startswith("CVE-")]
        items.append({"id": v.get("id"), "cves": cves, "summary": (v.get("summary") or v.get("details") or "")[:240],
                      "severity": _severity(v), "fixed_in": _fixed(v, package), "published": v.get("published")})
    kev = in_kev([c for i in items for c in i["cves"]]) if items else {}
    for i in items:
        i["in_kev"] = any(c in kev for c in i["cves"])
    items.sort(key=lambda i: i["published"] or "", reverse=True)    # 新的在前
    items.sort(key=lambda i: not i["in_kev"])                         # 稳定排序：在野利用的再提到最前
    return {"ecosystem": ecosystem, "package": package, "version": version, "count": len(items), "vulns": items[:40],
            "truncated": len(items) > 40}


def epss(cve_ids):
    ids = [c.upper() for c in (cve_ids or []) if c][:100]
    if not ids:
        return {}
    r = requests.get(EPSS_URL, params={"cve": ",".join(ids)}, timeout=_TIMEOUT)
    r.raise_for_status()
    return {x["cve"]: {"epss": float(x["epss"]), "percentile": float(x["percentile"])} for x in r.json().get("data") or []}


def detail(vuln_id):
    """单个漏洞详情（OSV 支持 CVE / GHSA / GO / PYSEC 等 id）+ EPSS + 是否在 KEV。"""
    vid = (vuln_id or "").strip()
    if not vid:
        return {"error": "缺少漏洞 id"}
    r = requests.get(OSV_VULN.format(vid), timeout=_TIMEOUT)
    if r.status_code == 404:
        out = {"id": vid, "osv": None}
    else:
        r.raise_for_status()
        v = r.json()
        out = {"id": v.get("id"), "aliases": v.get("aliases"), "summary": v.get("summary"),
               "details": (v.get("details") or "")[:1500], "severity": _severity(v), "published": v.get("published"),
               "affected": [{"package": (a.get("package") or {}).get("name"),
                             "ecosystem": (a.get("package") or {}).get("ecosystem"),
                             "fixed_in": _fixed({"affected": [a]}, None)} for a in (v.get("affected") or [])][:10],
               "references": [x.get("url") for x in (v.get("references") or [])][:8]}
    cves = [vid.upper()] if vid.upper().startswith("CVE-") else [a for a in (out.get("aliases") or []) if a.startswith("CVE-")]
    if cves:
        out["kev"] = in_kev(cves)
        try:
            out["epss"] = epss(cves)
        except Exception as e:
            out["epss_error"] = str(e)
    return out
