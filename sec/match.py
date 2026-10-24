"""漏洞 ↔ 资产匹配与报告数据（确定性）。模型只负责把结果写成文字，不决定「相不相关」和优先级。

匹配：
  带 ecosystem:package 的情报（GHSA/OSV）只按组件源码包精确匹配（core.components.PACKAGES）；
  其余（KEV、厂商公告）按组件关键词在产品/标题/描述里的词边界匹配，并压掉被更具体组件覆盖的泛称。
版本判断：OSV 的修复版本（fixed）对照我们在跑的版本；没有引入版本信息，只能「按修复版本推断」。
优先级：
  P0 在野利用（KEV）+ 我们在跑 + 版本受影响或无法确认
  P1 版本受影响 + （CVSS≥7 / 严重度高或严重 / EPSS≥10%）
  P2 版本受影响或无法确认
  P3 我们的版本不低于修复版本（大概率不受影响，仍列出依据）
"""
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from core import assetstore, components, store
from . import feeds

_CACHE_TTL = 86400


def _when(ts):
    """时间戳 → 北京时间文本（给模型直接引用，避免它自己换算出错）。"""
    if not ts:
        return None
    from datetime import datetime, timedelta, timezone
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
_RANGE = re.compile(r"(?:before|prior to|earlier than|<)\s*v?(\d+(?:\.\d+){1,3})", re.I)
# 厂商补丁公告标题常直接列出修复版本，如 "Patch Release: 19.2.4, 19.3.2, 19.1.10"
_RELEASE = re.compile(r"(?:patch release|security release|fixed in|upgrade to|update to)[:：]?\s*"
                      r"((?:v?\d+\.\d+(?:\.\d+)*(?:(?:\s*(?:,|、|and|和|及)\s*)+)?)+)", re.I)
_REAL_VERSION = re.compile(r"v?\d+(?:\.\d+)+(?:[-+.][0-9A-Za-z.]+)?")


def _detail(cve):
    """OSV 详情（缓存一天）；失败返回 None，不阻塞报告。"""
    key = f"osv_cache:{cve}"
    try:
        cached = json.loads(store.get_setting(key) or "null")
        if cached and time.time() - cached.get("_at", 0) < _CACHE_TTL:
            return cached
    except ValueError:
        pass
    try:
        d = feeds.detail(cve)
    except Exception:
        return None
    if isinstance(d, dict) and not d.get("error"):
        d["_at"] = time.time()
        store.set_setting(key, json.dumps(d, ensure_ascii=False)[:20000])
        return d
    return None


_ECOSYSTEMS = {"go": "Go", "npm": "npm", "pip": "PyPI", "pypi": "PyPI", "maven": "Maven", "rust": "crates.io",
               "crates.io": "crates.io", "composer": "Packagist", "rubygems": "RubyGems", "gem": "RubyGems",
               "nuget": "NuGet", "hex": "Hex", "pub": "Pub", "swift": "SwiftURL"}


def _osv_package(product, version):
    """OSV「这个包的这个版本受哪些漏洞影响」（含引入/修复区间，缓存半天）→ {CVE: [修复版本]}；查询失败返回 None。"""
    eco, _, pkg = product.partition(":")
    eco = _ECOSYSTEMS.get(eco.lower())
    if not (eco and pkg and version):
        return None
    key = f"osvq:{eco}:{pkg}:{version}"
    try:
        cached = json.loads(store.get_setting(key) or "null")
        if cached and time.time() - cached.get("_at", 0) < _CACHE_TTL / 2:
            return cached["hits"]
    except ValueError:
        pass
    try:
        r = feeds.osv_query(eco, pkg, version)
    except Exception:
        return None
    if not isinstance(r, dict) or r.get("error"):
        return None
    hits = {}
    for v in r.get("vulns") or []:
        for cve in v.get("cves") or []:
            hits.setdefault(cve, sorted(set(hits.get(cve, []) + list(v.get("fixed_in") or []))))
    store.set_setting(key, json.dumps({"_at": time.time(), "hits": hits}))
    return hits


def verdict(ours, fixed):
    """我们的版本对照修复版本列表 → (affected / fixed / unknown, 说明)。"""
    o = components.parse_version(ours)
    # 只认形如 1.2.3 的版本号；OSV 的 GIT 生态会给提交哈希，不能当版本比较
    fs = sorted({v for v in (components.parse_version(f) for f in fixed or [] if _REAL_VERSION.fullmatch(str(f))) if v})
    if not o:
        return "unknown", "版本无法从镜像 tag 解析（浮动 tag 或提交号）"
    if not fs:
        return "unknown", "情报未给出修复版本"
    line = [f for f in fs if f[:2] == o[:2]]
    if line:
        f = min(line)
        return ("affected", f"同一版本线修复于 {'.'.join(map(str, f))}") if o < f else \
               ("fixed", f"不低于同一版本线修复版本 {'.'.join(map(str, f))}")
    major = [f for f in fs if f[0] == o[0]]
    if major:
        if all(f <= o for f in major):
            return "fixed", f"高于本大版本所有修复版本（{', '.join('.'.join(map(str, f)) for f in major)}）"
        return "affected", f"本大版本修复于 {'.'.join(map(str, min(f for f in major if f > o)))}"
    if all(f[0] < o[0] for f in fs):
        return "fixed", "所在大版本比所有修复版本都新"
    if all(f[0] > o[0] for f in fs):
        return "affected", f"所在大版本没有修复，最近修复在 {'.'.join(map(str, fs[0]))}"
    return "unknown", "修复版本分布无法判断"


def _fixed_for(detail, component, package_product):
    if not detail:
        return []
    out = []
    pkg = package_product.split(":", 1)[1] if package_product and ":" in package_product else None
    for a in detail.get("affected") or []:
        if pkg and (a.get("package") or "") != pkg:
            continue
        out += a.get("fixed_in") or []
    return out


_CVSS_W = {"AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}, "AC": {"L": 0.77, "H": 0.44},
           "UI": {"N": 0.85, "R": 0.62}, "C": {"H": 0.56, "L": 0.22, "N": 0}, "I": {"H": 0.56, "L": 0.22, "N": 0},
           "A": {"H": 0.56, "L": 0.22, "N": 0}}


def _roundup(x):
    i = round(x * 100000)
    return i / 100000.0 if i % 10000 == 0 else (int(i / 10000) + 1) / 10.0


def cvss3_score(vector):
    """CVSS v3.0/3.1 基础分（规范公式）；不是合法 v3 向量时返回 None。"""
    if not isinstance(vector, str) or not vector.startswith("CVSS:3"):
        return None
    try:
        m = dict(part.split(":", 1) for part in vector.split("/")[1:])
        scope_changed = m["S"] == "C"
        pr = {"N": 0.85, "L": 0.68 if scope_changed else 0.62, "H": 0.5 if scope_changed else 0.27}[m["PR"]]
        iss = 1 - (1 - _CVSS_W["C"][m["C"]]) * (1 - _CVSS_W["I"][m["I"]]) * (1 - _CVSS_W["A"][m["A"]])
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if scope_changed else 6.42 * iss
        exploit = 8.22 * _CVSS_W["AV"][m["AV"]] * _CVSS_W["AC"][m["AC"]] * pr * _CVSS_W["UI"][m["UI"]]
        if impact <= 0:
            return 0.0
        return _roundup(min(1.08 * (impact + exploit), 10)) if scope_changed else _roundup(min(impact + exploit, 10))
    except (KeyError, ValueError):
        return None


def _label(score):
    if score is None:
        return None
    return "严重" if score >= 9 else "高危" if score >= 7 else "中危" if score >= 4 else "低危" if score > 0 else "无"


def _severity_rank(cvss, severity):
    if isinstance(cvss, (int, float)) and cvss >= 7:
        return True
    return str(severity or "").upper() in ("HIGH", "CRITICAL")


def _priority(kev, verdicts, cvss, severity, epss, runtime):
    """返回 (优先级, 最差判定, 依据)。"""
    worst = "affected" if "affected" in verdicts else "unknown" if "unknown" in verdicts else "fixed"
    if kev and runtime and worst != "fixed":
        return "P0", worst, "在野利用（KEV）+ 线上在跑 + 版本受影响或无法确认"
    if worst == "affected" and (_severity_rank(cvss, severity) or (epss or 0) >= 0.1):
        return "P1", worst, "版本确认受影响 + 高危/严重或利用概率≥10%"
    if worst != "fixed":
        return "P2", worst, "版本受影响但危害较低，或版本无法确认"
    return "P3", worst, "我们的版本不低于修复版本"


_DOWNGRADE = {"P0": "P1", "P1": "P2"}


def _is_init(asset):
    return "init 容器" in (asset.get("workload") or "")


_SEV_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}


def image_findings(assets, limit=20, kev=None):
    """Harbor 扫描结果（只含 assets 里在跑的镜像）：每个镜像的严重度统计、可修复的高危/严重、命中 KEV 的 CVE。"""
    by_image = {}
    for a in assets:
        by_image.setdefault(a["image"], []).append(a)
    scans = [s for s in assetstore.list_scans() if s["image"] in by_image]
    kev = kev if kev is not None else {r[0] for r in store._conn.execute("SELECT cve FROM vulns WHERE kev=1")}
    coverage = {"images": len(scans), "scanned": 0, "not_scanned": [], "errors": []}
    items = []
    for s in scans:
        status = (s["status"] or "").lower()
        name = f"{s['project']}/{s['repo']}"
        if status != "success":
            (coverage["errors"] if status == "error" else coverage["not_scanned"]).append(
                name + (f"（{s['error']}）" if s["error"] else f"（{s['status']}）"))
            continue
        coverage["scanned"] += 1
        vulns = assetstore.image_vulns(s["project"], s["repo"], s["reference"])
        fixable = [v for v in vulns if v["fixed"] and v["severity"] in ("Critical", "High")]
        hit_kev = sorted({v["cve"] for v in vulns if v["cve"] in kev})
        crit_fix = sum(1 for v in fixable if v["severity"] == "Critical")
        prio = "P0" if hit_kev else "P1" if crit_fix else "P2" if fixable else "P3"
        pkgs = {}
        for v in sorted(fixable, key=lambda v: (_SEV_ORDER.get(v["severity"], 9), -(v["cvss"] or 0))):
            p = pkgs.setdefault(v["package"], {"package": v["package"], "version": v["version"], "fixed": v["fixed"],
                                                "severity": v["severity"], "cves": []})
            p["cves"].append(v["cve"])
        mine = by_image[s["image"]]
        items.append({
            "priority": prio, "image": s["image"], "service": mine[0]["component"],
            "workloads": sorted({f"{a['cluster']}/{a['namespace']}/{a['workload'].split(' · ')[0]}" for a in mine})[:4],
            "counts": {k: v for k, v in (s["summary"] or {}).items() if v},
            "total": s["total"], "fixable_high_critical": len(fixable), "kev_cves": hit_kev,
            "fix_packages": list(pkgs.values())[:6],
            "advice": ("先升级上面列出的包或基础镜像到修复版本，重新构建；" if fixable else "无可修复的高危/严重；")
                      + ("其中有在野利用漏洞，优先处理。" if hit_kev else ""),
        })
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    items.sort(key=lambda x: (order[x["priority"]], -x["fixable_high_critical"]))
    fetched = max([s["fetched"] for s in scans] or [0])
    return {"coverage": coverage, "fetched_at": _when(fetched), "items": items[:limit],
            "by_priority": dict(Counter(x["priority"] for x in items))}


def report(days=7, visible=None, enrich=True, limit=30, new_only=False):
    """报告数据：资产清单状态、情报统计、相关条目（带证据）、无关条目统计。visible(asset)->bool 做权限过滤。
    new_only=True（日报）：只看窗口内首次出现的漏洞，不重复报告此前已报过的。"""
    assets = [a for a in assetstore.list_assets(limit=20000) if visible is None or visible(a)]
    srcs = assetstore.sources()
    inventory = {"sources": [{"source": s["source"], "status": s["status"], "assets": s["count"], "error": s["error"],
                              "last_attempt": _when(s["refreshed"]), "last_success": _when(s["ok_at"])} for s in srcs],
                 "assets": len(assets)}
    oldest_ok = min([s["ok_at"] for s in srcs if s["ok_at"]] or [0])
    warnings = []
    if not assets:
        warnings.append("资产清单为空：先调用 asset_refresh 采集（需要能连上集群），否则无法判断相关性")
    if any(s["status"] != "ok" for s in srcs):
        warnings.append("部分资产来源最近一次采集失败，相关数据可能过期：" +
                        "；".join(f"{s['source']}：{s['error']}" for s in srcs if s["status"] != "ok"))
    if oldest_ok and time.time() - oldest_ok > 86400:
        warnings.append(f"资产清单已 {int((time.time() - oldest_ok) / 3600)} 小时未成功刷新")

    by_comp = {}
    for a in assets:
        if a["kind"] != "service":
            by_comp.setdefault(a["component"], []).append(a)
    since = time.time() - int(days) * 86400
    column = "first_seen" if new_only else "last_seen"
    rows = store._conn.execute(f"SELECT * FROM vulns WHERE {column}>=? ORDER BY {column} DESC", (since,)).fetchall()

    candidates, unrelated = [], []
    for r in rows:
        v = dict(r)
        products = json.loads(v.get("products") or "[]")
        pkgs = [p for p in products if components.is_package(p)]
        hits = {}
        if pkgs:
            for p in pkgs:
                comp = components.package_component(p, by_comp)
                if comp:
                    hits[comp] = p
        else:
            text = " ".join(products + [v.get("title") or "", (v.get("description") or "")[:800]])
            hits = {c: kw for c, kw in components.mentioned(by_comp, text).items()}
        if not hits:
            unrelated.append(v)
            continue
        for comp, via in hits.items():
            candidates.append({"v": v, "component": comp, "via": via, "package": via if components.is_package(via) else None})

    details = {}
    if enrich and candidates:
        cves = sorted({c["v"]["cve"] for c in candidates})[:limit]
        with ThreadPoolExecutor(max_workers=6) as pool:
            for cve, d in zip(cves, pool.map(_detail, cves)):
                details[cve] = d

    related, needs_confirm = [], []
    for c in candidates:
        v, comp = c["v"], c["component"]
        d = details.get(v["cve"])
        fixed = _fixed_for(d, comp, c["package"])
        fixed = [f for f in fixed if _REAL_VERSION.fullmatch(str(f))]
        if not fixed and not c["package"]:
            text = f"{v.get('title') or ''} {v.get('description') or ''}"
            fixed = _RANGE.findall(text)                               # 厂商公告文本里的 "before 1.2.3"
            for group in _RELEASE.findall(text):
                fixed += re.findall(r"\d+\.\d+(?:\.\d+)*", group)
        mine = by_comp[comp]
        sub = None if c["package"] else components.subproduct(comp, f"{v.get('title') or ''} {(v.get('description') or '')[:400]}")
        if sub and not any(sub.replace(" ", "-") in a["component"] or sub in (a["image"] or "").lower() for a in mine):
            # 公告针对子产品，而资产里没有它：版本号不适用于组件本体，放进「需确认」而不是「相关」
            needs_confirm.append({"cve": v["cve"], "title": v.get("title"), "component": comp, "subproduct": sub,
                                  "kev": bool(v.get("kev")), "summary": (v.get("description") or "")[:240],
                                  "why": f"公告针对「{comp} {sub}」，资产清单里没有发现该子产品；如未部署可判为不受影响",
                                  "sources": json.loads(v.get("sources") or "[]")[:3]})
            continue
        versions = sorted({a["version"] for a in mine if a["version"]},
                          key=lambda x: components.parse_version(x) or ()) or [""]
        hits_by = {ver: (_osv_package(c["package"], ver) if c["package"] and ver else None) for ver in versions}
        known = any(h and v["cve"] in h for h in hits_by.values())       # OSV 收录了该漏洞对这个包的影响区间
        for h in hits_by.values():
            if h and v["cve"] in h:
                fixed = sorted(set(fixed + [f for f in h[v["cve"]] if _REAL_VERSION.fullmatch(str(f))]))
        checks = []
        for ver in versions:
            hits = hits_by.get(ver)
            if hits is not None and v["cve"] in hits:
                fix = [f for f in hits[v["cve"]] if _REAL_VERSION.fullmatch(str(f))]
                same = verdict(ver, fix)[1] if fix else "OSV 未给出修复版本"
                checks.append({"version": ver, "verdict": "affected", "why": f"OSV 确认该版本受影响；{same}"})
            elif hits is not None and known:
                checks.append({"version": ver, "verdict": "fixed",
                               "why": "OSV 按包版本查询：该版本不在受影响区间" + (f"（修复版本 {', '.join(fixed[:4])}）" if fixed else "")})
            elif hits is not None and fixed:
                checks.append({"version": ver, "verdict": verdict(ver, fixed)[0] if verdict(ver, fixed)[0] != "affected" else "unknown",
                               "why": "OSV 按包版本查询未命中该漏洞；" + verdict(ver, fixed)[1]})
            elif hits is not None:
                checks.append({"version": ver, "verdict": "unknown",
                               "why": "OSV 尚未收录该漏洞对这个包的影响范围（新漏洞常见），需看原始公告"})
            else:
                checks.append({"version": ver or "（未知）", "verdict": verdict(ver, fixed)[0], "why": verdict(ver, fixed)[1]})
        affected_assets = [a for a in mine if any(ch["verdict"] == "affected" and ch["version"] == a["version"] for ch in checks)]
        severity = v.get("severity") or (d or {}).get("severity")
        vector = severity if isinstance(severity, str) and severity.startswith("CVSS:") else None
        cvss = v.get("cvss") if isinstance(v.get("cvss"), (int, float)) else cvss3_score(vector)
        if vector:
            severity = None
        runtime = any("运行时" in a["evidence"] or a["kind"] == "component" for a in mine)
        prio, worst, reason = _priority(bool(v.get("kev")), [x["verdict"] for x in checks], cvss, severity, v.get("epss"), runtime)
        shown = affected_assets or mine
        exposure = None
        if shown and all(_is_init(a) for a in shown):
            # 只出现在 init 容器里：通常只在启动时运行或为主进程提供程序文件，漏洞功能多半不会被执行
            exposure = ("受影响版本只出现在 init 容器中（启动时运行，或为主容器提供程序文件）；"
                        "需确认漏洞所在功能是否会被执行，确认前不按高优先级处理")
            if prio in _DOWNGRADE:
                prio, reason = _DOWNGRADE[prio], reason + "；仅 init 容器，降一级"
        related.append({
            "priority": prio, "priority_reason": reason, "exposure": exposure,
            "cve": v["cve"], "title": v.get("title"), "component": comp,
            "summary": (v.get("description") or "")[:300],
            "kev": bool(v.get("kev")), "epss": v.get("epss"),
            "cvss_score": cvss, "cvss_vector": vector, "severity": severity or _label(cvss),
            "matched_by": ("源码包 " if c["package"] else "关键词 ") + c["via"],
            "confidence": ("高：按组件源码包精确匹配" if c["package"] else
                           "中：按产品名关键词匹配，需确认公告针对的子组件/功能（如插件、网关、Runner）在我们这里是否启用"),
            "fixed_versions": sorted(set(fixed))[:8], "version_checks": checks, "overall": worst,
            "assets": [{k: a[k] for k in ("cluster", "namespace", "workload", "app", "image", "version", "evidence") if a[k]}
                       for a in (affected_assets or mine)[:6]],
            "assets_affected": len(affected_assets),
            "assets_total": len(mine),
            "sources": json.loads(v.get("sources") or "[]")[:4],
            "ledger_status": v.get("status"),
        })
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    related.sort(key=lambda x: (order[x["priority"]], -(x["epss"] or 0)))

    vendors = Counter()
    for v in unrelated:
        prods = json.loads(v.get("products") or "[]")
        vendors[prods[0] if prods else "（无产品信息）"] += 1
    own = sorted({a["component"] for a in assets if a["kind"] == "service"})
    images = image_findings(assets, limit=limit) if assetstore.list_scans() else None
    return {
        "window_days": int(days), "mode": "只含窗口内新出现的漏洞（日报）" if new_only else "窗口内出现过的漏洞",
        "generated_at": _when(time.time()) + "（北京时间）", "inventory": inventory,
        "warnings": warnings,
        "intel": {"in_window": len(rows), "kev_in_window": sum(1 for r in rows if r["kev"])},
        "related": related[:limit], "related_total": len(related),
        "needs_confirmation": needs_confirm[:limit],
        "related_by_priority": dict(Counter(x["priority"] for x in related)),
        "unrelated": {"count": len(unrelated), "kev": sum(1 for v in unrelated if v.get("kev")),
                      "top_products": vendors.most_common(8)},
        "self_built_images": {"count": len(own), "names": own[:20],
                              "note": ("自研镜像与经 Harbor 代理的公共镜像，扫描结果见 image_findings" if images else
                                       "自研镜像的依赖与基础镜像漏洞需要 Harbor 镜像扫描（未配置 Harbor 或尚无扫描结果），本报告未覆盖")},
        "image_findings": images,
        "method": "确定性匹配：带包名的情报按组件源码包精确匹配，其余按组件关键词；版本按 OSV 修复版本推断（无引入版本信息）。",
    }
