"""GitLab 只读集成：搜项目、列目录、读文件（REST API，只发 GET）。

连接存 kv：gitlab_url / gitlab_token（建议 scope 只勾 read_api 的个人或组访问令牌，按需限制到相关 group）。
和「代码仓库」（harness.workspace，要改代码时克隆 + 提 MR）、「技能源」（同步 SKILL.md）是三件事：
这里只给数字员工「看」GitLab 上的东西，不克隆、不写。
"""
from urllib.parse import quote

import requests

from core import store

_TIMEOUT = 20
_MAX_FILE = 200 * 1024


def config():
    return {"url": (store.get_setting("gitlab_url") or "").rstrip("/"), "token": store.get_setting("gitlab_token") or ""}


def _get(path, params=None, raw=False):
    c = config()
    if not (c["url"] and c["token"]):
        return None, {"error": "GitLab 还没配置（后台「连接凭证」页填地址和 read_api 令牌）"}
    try:
        r = requests.get(f"{c['url']}/api/v4{path}", params=params, headers={"PRIVATE-TOKEN": c["token"]},
                         timeout=_TIMEOUT)
    except requests.RequestException as e:
        return None, {"error": f"连不上 GitLab：{type(e).__name__}: {str(e)[:200]}"}
    if r.status_code in (401, 403):
        return None, {"error": f"GitLab 拒绝访问（{r.status_code}），令牌可能失效或无权限"}
    if r.status_code == 404:
        return None, {"error": "GitLab 上找不到（项目/分支/路径不存在，或令牌看不到它）"}
    if r.status_code >= 300:
        return None, {"error": f"GitLab 返回 {r.status_code}：{r.text[:200]}"}
    return (r.content if raw else r.json()), None


def _pid(project):
    return quote(str(project).strip().strip("/"), safe="") if not str(project).isdigit() else str(project)


def projects(search=None, limit=50):
    data, err = _get("/projects", {"search": search or None, "simple": "true", "per_page": min(int(limit or 50), 100),
                                   "order_by": "last_activity_at", "membership": "true"})
    if err:
        return err
    return {"projects": [{"id": p.get("id"), "path": p.get("path_with_namespace"),
                          "default_branch": p.get("default_branch"), "web_url": p.get("web_url"),
                          "last_activity": p.get("last_activity_at")} for p in data]}


def tree(project, path="", ref=None):
    data, err = _get(f"/projects/{_pid(project)}/repository/tree",
                     {"path": path or None, "ref": ref or None, "per_page": 100})
    if err:
        return err
    return {"project": project, "path": path or ".", "entries": [
        {"path": e.get("path"), "type": "dir" if e.get("type") == "tree" else "file"} for e in data]}


def read(project, path, ref=None):
    if not path:
        return {"error": "缺少文件路径"}
    data, err = _get(f"/projects/{_pid(project)}/repository/files/{quote(path, safe='')}/raw",
                     {"ref": ref or "HEAD"}, raw=True)
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
