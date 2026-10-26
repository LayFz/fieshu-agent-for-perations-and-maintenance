"""安全漏洞日报：直接由数据生成飞书卡片（不经模型改写），数字、版本与概览页一致。

记忆：每次生成保存当天快照（kv vuln_daily_snapshots，保留 14 天），与「今天之前最近一天」比较，
给出「较昨日」变化：哪些镜像修好了 / 新出现问题、P1/P2 数量变化、清掉了多少 CVE。
"""
import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from core import assetstore, store
from . import match

_TZ = ZoneInfo("Asia/Shanghai")
_KEEP_DAYS = 14
INSTRUCTION = "/安全日报"


def _today():
    return datetime.now(_TZ).strftime("%Y-%m-%d")


def _image_cves(items):
    """每个有问题镜像的可修复严重/高危 CVE 集合（去重），用于跨天比较。"""
    refs = {s["image"]: s for s in assetstore.list_scans()}
    out = {}
    for it in items:
        s = refs.get(it["image"])
        if not s:
            continue
        cves = {v["cve"] for v in assetstore.image_vulns(s["project"], s["repo"], s["reference"])
                if v["fixed"] and v["severity"] in ("Critical", "High")}
        out[it["image"]] = {"service": it["service"], "priority": it["priority"], "cves": sorted(cves)}
    return out


def _load():
    try:
        data = json.loads(store.get_setting("vuln_daily_snapshots") or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _save(today, snap):
    data = _load()
    data[today] = snap
    for day in sorted(data)[:-_KEEP_DAYS]:
        del data[day]
    store.set_setting("vuln_daily_snapshots", json.dumps(data, ensure_ascii=False))


def diff(prev, snap):
    """两天快照比较：镜像修好 / 新出现问题、清掉与新增的 CVE 数。"""
    if not prev:
        return None
    old, new = prev.get("images", {}), snap["images"]
    old_cves = {c for v in old.values() for c in v["cves"]}
    new_cves = {c for v in new.values() for c in v["cves"]}
    return {"date": prev.get("date"),
            "by_priority": {p: snap["by_priority"].get(p, 0) - prev.get("by_priority", {}).get(p, 0) for p in ("P0", "P1", "P2")},
            "fixed_images": sorted({old[i]["service"] for i in old if i not in new}),
            "new_images": sorted({new[i]["service"] for i in new if i not in old}),
            "cleared_cves": len(old_cves - new_cves), "new_cves": len(new_cves - old_cves)}


def _ver_key(v):
    return [int(x) for x in re.findall(r"\d+", v or "")]


_DOMAIN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com\.cn|net\.cn|com|net|cn|io|org)\b", re.I)
_SHORT_REACH = {"可能经公网入口间接到达": "经公网入口间接", "仅集群内部可达": "仅集群内", "仅内网可达": "仅内网"}


def _where(item):
    """暴露面一句话：公网可达｜skill.upmore.com.cn；远端集群的写 VKE Ingress 名。"""
    note = item.get("reachability_note") or ""
    reach = _SHORT_REACH.get(item["reachability"], item["reachability"])
    m = _DOMAIN.search(note)
    if m:
        return f"{reach}｜{m.group(0)}"
    ing = re.search(r"Ingress ([^\s（(]+)", note)
    return f"{reach}｜远端集群 Ingress {ing.group(1)}" if ing else reach


def _nearest(current, fixed):
    """多条修复版本线（如 Django 5.2.8, 5.1.14, 4.2.26）里取不低于当前版本的最小一个，即同线最近的修复。"""
    options = [x.strip() for x in (fixed or "").split(",") if x.strip()]
    cur = _ver_key(current)
    above = [x for x in options if _ver_key(x) >= cur]
    return min(above, key=_ver_key) if above else (max(options, key=_ver_key) if options else "")


def _md(text):
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def _num(value, label, delta=None):
    d = "" if not delta else f" <font color='{'red' if delta > 0 else 'green'}'>{'+' if delta > 0 else ''}{delta}</font>"
    return {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top",
            "elements": [_md(f"**{value}**{d}\n<font color='grey'>{label}</font>")]}


def build(report, findings, outside, prev=None, base_url=""):
    """report = match.report(days=1, new_only=True)；findings = match.image_findings(全部)。返回 (card, snapshot)。"""
    items, cov, bp = findings["items"], findings["coverage"], findings["by_priority"]
    snap = {"date": _today(), "by_priority": {p: bp.get(p, 0) for p in ("P0", "P1", "P2", "P3")},
            # 记录真实修复状态（不受标记影响）：标记只是不提醒，不能在「较昨日」里算成已修好
            "images": {k: v for k, v in _image_cves(items).items() if v["cves"]}}
    change = diff(prev, snap)
    related = report.get("related") or []
    intel = report.get("intel") or {}
    uncovered = len(cov.get("not_in_harbor", [])) + len(cov.get("not_scanned", [])) + len(cov.get("errors", [])) + len(outside)

    if related and any(r["priority"] in ("P0", "P1") for r in related) or bp.get("P0"):
        color, verdict = "red", "今天有需要处理的新风险"
    elif related:
        color, verdict = "orange", "今天有与我们相关的新漏洞，优先级不高"
    else:
        color, verdict = ("orange" if bp.get("P1") else "green"), "今天没有影响我们的新漏洞"

    els = [_md(f"**{verdict}**　今日新增情报 {intel.get('in_window', 0)} 条（在野利用 {intel.get('kev_in_window', 0)}），"
               f"与我们线上资产相关 **{report.get('related_total', 0)}** 条")]
    for r in related[:5]:
        where = "、".join(sorted({a.get("workload", "").split(" · ")[0] or a.get("image", "") for a in r["assets"]}))[:80]
        els.append(_md(f"　**{r['priority']}** {r['cve']} · {r['component']} · {r['reachability']}\n"
                       f"　<font color='grey'>{(r.get('title') or '')[:60]}｜影响 {where}｜修复版本 {', '.join(r['fixed_versions'][:3]) or '待确认'}</font>"))

    by = (change or {}).get("by_priority", {})
    els += [{"tag": "hr"},
            _md("**线上镜像现状**（Harbor 扫描；P1 只看应用依赖的严重漏洞，已结合暴露面与测试环境降级）"),
            {"tag": "column_set", "flex_mode": "none", "background_style": "grey", "columns": [
                _num(bp.get("P0", 0), "P0 在野利用", by.get("P0")), _num(bp.get("P1", 0), "P1 依赖有严重漏洞", by.get("P1")),
                _num(bp.get("P2", 0), "P2 高危/基础镜像", by.get("P2")), _num(uncovered, "未覆盖镜像")]}]
    if change:
        lines = [f"较 {change['date']}：清掉 {change['cleared_cves']} 个 CVE，新增 {change['new_cves']} 个"]
        if change["fixed_images"]:
            lines.append("✅ 已修好：" + "、".join(change["fixed_images"][:8]))
        if change["new_images"]:
            lines.append("🆕 新出现问题：" + "、".join(change["new_images"][:8]))
        els.append(_md("\n".join(lines)))
    else:
        els.append(_md("<font color='grey'>首份快照，明天起显示与前一天的变化</font>"))

    ms = findings.get("marks_summary") or {}
    if ms.get("applied") or ms.get("overdue"):
        names = {"planned": "计划修复", "accepted": "接受风险", "false_positive": "误报"}
        lines = []
        if ms.get("applied"):
            lines.append(f"<font color='grey'>已标记 {ms['applied']} 项不再重复提醒（"
                         + " · ".join(f"{names.get(k, k)} {v}" for k, v in ms["by_status"].items()) + "）</font>")
        for o in (ms.get("overdue") or [])[:5]:
            lines.append(f"<font color='red'>⏰ {o['service']} 的 {o['package']} 计划修复已逾期（原定 {o['until']}）</font>")
        els.append(_md("\n".join(lines)))

    urgent = [i for i in items if i["priority"] in ("P0", "P1")]
    if urgent:
        lines = [f"**要升级的应用依赖**（P0/P1 共 {len(urgent)} 个服务，列前 6；需开发改依赖后发布）"]
        for it in urgent[:6]:
            fc = it.get("fixable_cves") or {}
            groups = {}
            for pk in it.get("app_packages") or []:      # 同版本同修复的子包合成一条
                groups.setdefault((pk["version"], pk["fixed"]), []).append(pk["package"])
            pk = "、".join(f"{'/'.join(names)} {ver} → {_nearest(ver, fixed)}" for (ver, fixed), names in list(groups.items())[:3])
            lines.append(f"• **{it['service']}** · 依赖 {fc.get('app_critical', 0)} 严重 {fc.get('app_high', 0)} 高危"
                         f" · <font color='grey'>{_where(it)}</font>\n"
                         f"　<font color='grey'>{pk}</font>")
        els += [{"tag": "hr"}, _md("\n".join(lines))]

    rb = findings.get("rebuild") or {}
    if rb.get("images"):
        base_pkgs = [c["package"] for c in findings.get("common_fixes") or [] if c.get("layer") == "base"][:4]
        els += [{"tag": "hr"}, _md(
            f"**批量重建镜像**　{rb['images']} 个镜像的基础镜像有可修复漏洞（{rb['critical']} 严重、{rb['high']} 高危，"
            f"其中公网相关 {len(rb.get('public') or [])} 个）\n"
            f"<font color='grey'>{rb.get('advice', '')}；主要是 {'、'.join(base_pkgs)}</font>")]

    if uncovered:
        names = [n.rsplit("/", 1)[-1] for n in cov.get("not_in_harbor", [])]
        others = [re.sub(r"[:@].*$", "", n).rsplit("/", 1)[-1] for n in outside]
        els += [{"tag": "hr"}, _md(
            f"**扫描覆盖缺口** {uncovered} 个镜像没有扫描结果，漏洞未计入上面的数字\n"
            f"<font color='grey'>Harbor 缓存已清理 {len(names)}：{'、'.join(names[:6])}｜不经 Harbor {len(outside)}："
            f"{'、'.join(sorted(set(others))[:6])}…</font>")]

    els.append({"tag": "note", "elements": [{"tag": "plain_text", "content":
        f"扫描数据 {findings.get('fetched_at') or '—'} · 生成 {datetime.now(_TZ).strftime('%m-%d %H:%M')}（北京时间） · 漏洞数按去重 CVE"}]})
    if base_url:
        els.append({"tag": "action", "actions": [{"tag": "button", "type": "primary",
                    "text": {"tag": "plain_text", "content": "打开安全概览"}, "url": base_url.rstrip("/") + "/#security"}]})
    card = {"config": {"wide_screen_mode": True},
            "header": {"template": color, "title": {"tag": "plain_text",
                       "content": f"🛡 安全漏洞日报 · {datetime.now(_TZ).strftime('%m月%d日')}"}},
            "elements": els}
    return card, snap


def run(visible=None):
    """生成今日卡片并记下快照；返回卡片。"""
    assets = [a for a in assetstore.list_assets(limit=20000) if visible is None or visible(a)]
    report = match.report(days=1, visible=visible, limit=10, new_only=True)
    findings = match.image_findings(assets, limit=500)
    history = _load()
    today = _today()
    prev = next((history[d] for d in sorted(history, reverse=True) if d < today), None)
    card, snap = build(report, findings, match.outside_harbor(assets), prev,
                      store.get_setting("admin_base_url") or "http://127.0.0.1:8800")
    _save(today, snap)
    return card
