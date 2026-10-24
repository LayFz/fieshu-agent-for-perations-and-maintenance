"""Harbor 只读集成：镜像扫描概览与漏洞明细（Trivy 等扫描器的结果）。

连接存 kv：harbor_url（默认 https://harbor.upmore.net）/ harbor_username（建议 robot$ 只读账号）/ harbor_password。
只发 GET；唯一的写操作「触发扫描」默认关闭，需后台显式打开 harbor_trigger_scan=1，且只对未扫描的制品触发。
不继承环境代理；凭据只用于请求头，错误信息不回显响应体。
"""
from urllib.parse import quote

import requests

from core import store

_TIMEOUT = 20
_REPORT_TYPES = ("application/vnd.security.vulnerability.report; version=1.1",
                 "application/vnd.scanner.adapter.vuln.report.harbor+json; version=1.0")
_http = requests.Session()
_http.trust_env = False


def config():
    return {"url": (store.get_setting("harbor_url") or "https://harbor.upmore.net").rstrip("/"),
            "username": store.get_setting("harbor_username") or "",
            "password": store.get_setting("harbor_password") or "",
            "trigger_scan": store.get_setting("harbor_trigger_scan") == "1"}


def configured():
    c = config()
    return bool(c["url"] and c["username"] and c["password"])


def _request(method, path, params=None, accept=None):
    c = config()
    if not configured():
        return None, {"error": "Harbor 还没配置（后台「连接凭证」页填地址与只读 robot 账号）"}
    headers = {"Accept": accept or "application/json"}
    try:
        r = _http.request(method, c["url"] + "/api/v2.0" + path, params=params, headers=headers,
                          auth=(c["username"], c["password"]), timeout=_TIMEOUT, allow_redirects=False)
    except requests.RequestException as e:
        return None, {"error": f"连不上 Harbor：{type(e).__name__}"}
    if r.status_code in (401, 403):
        return None, {"error": f"Harbor 拒绝访问（{r.status_code}），robot 账号可能失效或缺少读取权限"}
    if r.status_code == 404:
        return None, {"error": "Harbor 上找不到该项目/仓库/制品"}
    if r.status_code >= 300:
        return None, {"error": f"Harbor 返回 HTTP {r.status_code}"}
    if not r.content:
        return {}, None
    try:
        return r.json(), None
    except ValueError:
        return None, {"error": "Harbor 返回的内容不是有效 JSON"}


def _repo(repo):
    # 仓库名里的 / 必须双重编码（Harbor API 约定）
    return quote(quote(repo, safe=""), safe="")


def artifact(project, repo, reference):
    """制品（含扫描概览）。reference 可以是 digest 或 tag。"""
    data, err = _request("GET", f"/projects/{quote(project, safe='')}/repositories/{_repo(repo)}/artifacts/"
                                f"{quote(reference, safe=':')}", params={"with_scan_overview": "true", "with_tag": "true"})
    return err or data


def scan_overview(art):
    """从制品取扫描状态与严重度汇总：{status, scanner, summary{Critical:n...}, total, fixable, completed}。"""
    for key in _REPORT_TYPES:
        so = (art.get("scan_overview") or {}).get(key)
        if so:
            summary = (so.get("summary") or {})
            return {"status": so.get("scan_status") or "", "scanner": ((so.get("scanner") or {}).get("name") or ""),
                    "summary": summary.get("summary") or {}, "total": summary.get("total"),
                    "fixable": summary.get("fixable"), "completed": so.get("end_time") or so.get("complete_percent")}
    return {"status": "not_scanned", "scanner": "", "summary": {}, "total": None, "fixable": None, "completed": None}


def vulnerabilities(project, repo, reference):
    """漏洞明细，归一成 [{cve, package, version, fixed, severity, cvss, vector, title}]。"""
    data, err = _request("GET", f"/projects/{quote(project, safe='')}/repositories/{_repo(repo)}/artifacts/"
                                f"{quote(reference, safe=':')}/additions/vulnerabilities")
    if err:
        return err
    report = next((data[k] for k in _REPORT_TYPES if isinstance(data, dict) and k in data), None)
    if report is None and isinstance(data, dict) and data:
        report = next(iter(data.values()))
    out = []
    for v in (report or {}).get("vulnerabilities") or []:
        cvss = v.get("preferred_cvss") or {}
        out.append({"cve": v.get("id") or "", "package": v.get("package") or "", "version": v.get("version") or "",
                    "fixed": v.get("fix_version") or "", "severity": v.get("severity") or "",
                    "cvss": cvss.get("score_v3"), "vector": cvss.get("vector_v3") or "",
                    "title": (v.get("description") or "")[:200]})
    return {"generated_at": (report or {}).get("generated_at"), "scanner": ((report or {}).get("scanner") or {}).get("name"),
            "vulnerabilities": out}


def trigger_scan(project, repo, reference):
    """触发扫描（写操作，默认关闭）。"""
    if not config()["trigger_scan"]:
        return {"error": "未开启自动触发扫描（kv harbor_trigger_scan）"}
    data, err = _request("POST", f"/projects/{quote(project, safe='')}/repositories/{_repo(repo)}/artifacts/"
                                 f"{quote(reference, safe=':')}/scan")
    return err or {"ok": True}


def test():
    data, err = _request("GET", "/projects", params={"page_size": 1})
    if err:
        return {"ok": False, **err}
    scanner, serr = _request("GET", "/scanners")
    default = next((s.get("name") for s in (scanner or []) if s.get("is_default")), None) if not serr else None
    return {"ok": True, "projects_visible": len(data or []) > 0, "default_scanner": default,
            "note": None if default else "没读到默认扫描器（未安装 Trivy，或 robot 账号无权查看扫描器）"}
