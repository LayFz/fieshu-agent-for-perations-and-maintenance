"""GitLab 只读集成：项目、目录、文件与流水线元数据（REST API，只发 GET）。

连接存 kv：gitlab_url / gitlab_token；巡检可改用 gitlab_project_tokens，为每个项目配置
项目访问令牌。只读巡检可用 read_api；同一项目需要审批后提 MR 时可用 Developer + api。
和「代码仓库」（harness.workspace，要改代码时克隆 + 提 MR）、「技能源」（同步 SKILL.md）是三件事：
这里只给数字员工「看」GitLab 上的东西，不克隆、不写。
"""
import json
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from core import store

_TIMEOUT = 20
_MAX_FILE = 200 * 1024


def config():
    return {"url": (store.get_setting("gitlab_url") or "").rstrip("/"), "token": store.get_setting("gitlab_token") or ""}


def project_tokens():
    try:
        tokens = json.loads(store.get_setting("gitlab_project_tokens", "{}") or "{}")
        return ({project: token for project, token in tokens.items()
                 if isinstance(project, str) and isinstance(token, str) and token}
                if isinstance(tokens, dict) else {})
    except ValueError:
        return {}


def has_token(project=None):
    return bool(project_tokens().get(str(project)) or config()["token"])


def _get(path, params=None, raw=False, with_headers=False, project=None):
    c = config()
    token = project_tokens().get(str(project)) if project is not None else None
    token = token or c["token"]
    if not (c["url"] and token):
        return None, {"error": "GitLab 还没配置（后台填地址与该项目的 API 访问令牌）"}
    try:
        r = requests.get(f"{c['url']}/api/v4{path}", params=params, headers={"PRIVATE-TOKEN": token},
                         timeout=_TIMEOUT, allow_redirects=False)
    except requests.RequestException as e:
        # 响应和异常都可能回显令牌或带凭据的 URL，仅报告异常类别/HTTP 状态。
        return None, {"error": f"连不上 GitLab：{type(e).__name__}"}
    if r.status_code in (401, 403):
        return None, {"error": f"GitLab 拒绝访问（{r.status_code}），令牌可能失效或无权限"}
    if r.status_code == 404:
        return None, {"error": "GitLab 上找不到（项目/分支/路径不存在，或令牌看不到它）"}
    if r.status_code >= 300:
        return None, {"error": f"GitLab 返回 HTTP {r.status_code}"}
    try:
        data = r.content if raw else r.json()
    except ValueError:
        return None, {"error": "GitLab 返回的内容不是有效 JSON"}
    return ((data, dict(r.headers)) if with_headers else data), None


def _safe_url(value):
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return None
        return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
    except ValueError:
        return None


def _page_number(value, maximum=None):
    if isinstance(value, bool) or not str(value).isdigit():
        raise ValueError("分页参数必须为正整数")
    number = int(value)
    if number < 1 or (maximum is not None and number > maximum):
        raise ValueError("页码必须大于零，每页/条数上限为 100")
    return number


def _pagination(headers, page, per_page, count):
    headers = {str(k).lower(): str(v) for k, v in headers.items()}
    value = headers.get("x-next-page")
    # 代理可能不保留分页头；满页不能当作完整结果。
    if value is None:
        next_page = page + 1 if count >= per_page else None
        source = "page_size"
    elif value == "":
        next_page, source = None, "header"
    elif value.isdigit() and int(value) > page:
        next_page, source = int(value), "header"
    else:
        next_page, source = page + 1, "invalid_header"
    total = headers.get("x-total", "")
    return {"page": page, "per_page": per_page, "next_page": next_page,
            "has_more": next_page is not None, "total": int(total) if total.isdigit() else None,
            "source": source}


def _pid(project):
    return quote(str(project).strip().strip("/"), safe="") if not str(project).isdigit() else str(project)


def projects(search=None, limit=50):
    data, err = _get("/projects", {"search": search or None, "simple": "true", "per_page": min(int(limit or 50), 100),
                                   "order_by": "last_activity_at", "membership": "true"})
    if err:
        return err
    return {"projects": [{"id": p.get("id"), "path": p.get("path_with_namespace"),
                          "default_branch": p.get("default_branch"), "web_url": _safe_url(p.get("web_url")),
                          "last_activity": p.get("last_activity_at")} for p in data]}


def tree(project, path="", ref=None, page=1, per_page=100):
    """一次返回一页；complete 只在本响应包含整个目录时为真。跨页宜固定 ref 为 SHA。"""
    try:
        page, per_page = _page_number(page), _page_number(per_page, 100)
    except ValueError as e:
        return {"error": str(e)}
    response, err = _get(f"/projects/{_pid(project)}/repository/tree",
                         {"path": path or None, "ref": ref or None, "page": page, "per_page": per_page},
                         with_headers=True, project=project)
    if err:
        return err
    data, headers = response
    if not isinstance(data, list) or any(not isinstance(e, dict) for e in data):
        return {"error": "GitLab 目录列表格式不正确"}
    pagination = _pagination(headers, page, per_page, len(data))
    return {"project": project, "path": path or ".", "ref": ref or "HEAD", "entries": [
        {"path": e.get("path"), "type": "dir" if e.get("type") == "tree" else "file",
         "id": e.get("id"), "mode": e.get("mode")} for e in data],
        "pagination": pagination, "complete": page == 1 and not pagination["has_more"]}


def project_details(project):
    """只输出项目定位、默认分支及其完整提交标识，不返回令牌/成员/作者信息。"""
    data, err = _get(f"/projects/{_pid(project)}", project=project)
    if err:
        return err
    if not isinstance(data, dict):
        return {"error": "GitLab 项目详情格式不正确"}
    out = {"id": data.get("id"), "path": data.get("path_with_namespace"),
           "name": data.get("name"), "default_branch": data.get("default_branch"),
           "web_url": _safe_url(data.get("web_url")), "archived": data.get("archived"),
           "empty_repo": data.get("empty_repo"), "last_activity": data.get("last_activity_at"),
           "default_branch_commit": None}
    branch = data.get("default_branch")
    if not branch:
        out["commit_status"] = "no_default_branch"
        return out
    data, err = _get(f"/projects/{_pid(project)}/repository/branches/{quote(str(branch), safe='')}", project=project)
    if err:
        return {**out, **err, "commit_status": "unavailable"}
    commit = data.get("commit") if isinstance(data, dict) else None
    if not isinstance(commit, dict) or not commit.get("id"):
        return {**out, "error": "GitLab 默认分支没有有效提交信息", "commit_status": "unavailable"}
    out["default_branch_commit"] = {k: commit.get(k) for k in ("id", "parent_ids", "created_at", "committed_date")}
    out["default_branch_commit"]["web_url"] = _safe_url(commit.get("web_url"))
    out["commit_status"] = "available"
    return out


def pipelines(project, ref=None, limit=20):
    """最近的一页流水线，按 id 降序；不读取 job trace、variables 或 artifacts。"""
    try:
        limit = _page_number(limit, 100)
    except ValueError as e:
        return {"error": str(e)}
    response, err = _get(f"/projects/{_pid(project)}/pipelines",
                         {"ref": ref or None, "page": 1, "per_page": limit, "order_by": "id", "sort": "desc"},
                         with_headers=True, project=project)
    if err:
        return err
    data, headers = response
    if not isinstance(data, list) or any(not isinstance(p, dict) for p in data):
        return {"error": "GitLab 流水线列表格式不正确"}
    records = []
    for p in data:
        record = {k: p.get(k) for k in ("id", "status", "ref", "sha", "created_at", "updated_at")}
        record["web_url"] = _safe_url(p.get("web_url"))
        records.append(record)
    pagination = _pagination(headers, 1, limit, len(data))
    return {"project": project, "ref": ref, "pipelines": records,
            "pagination": pagination, "complete": not pagination["has_more"]}


def read(project, path, ref=None):
    if not path:
        return {"error": "缺少文件路径"}
    data, err = _get(f"/projects/{_pid(project)}/repository/files/{quote(path, safe='')}/raw",
                     {"ref": ref or "HEAD"}, raw=True, project=project)
    if err:
        return err
    if b"\x00" in data[:8000]:
        return {"project": project, "path": path, "binary": True, "size": len(data)}
    text = data[:_MAX_FILE].decode("utf-8", "replace")
    return {"project": project, "path": path, "ref": ref or "HEAD", "content": text,
            "truncated": len(data) > _MAX_FILE}


def test():
    data, err = _get("/user")
    if err:
        return {"ok": False, **err}
    p = projects(limit=5)
    return {"ok": True, "user": data.get("username"), "visible_projects_sample": [x["path"] for x in p.get("projects", [])]}
