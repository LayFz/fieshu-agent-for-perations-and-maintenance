"""代码工作区：让数字员工在一次性工作区里改代码、跑测试、出 diff，最后由确定性代码提 MR。

边界：
  - 模型只能动工作区里的文件（路径解析后必须落在仓库目录内，不能碰 .git，符号链接跳出一律拒）。
  - diff 只看「模型通过工具改过的文件」（workspaces.changed），和 HEAD 比；跑测试生成的产物不进 MR。
  - 跑测试只能跑后台配置的命令（code_repos.test_cmd），清理环境变量、HOME 指到工作区；
    但仓库代码模型能改，所以跑测试等同执行任意代码 —— 工具元数据按 exec 处理，建议配「审批」预设。
  - 提 MR 不用 git push：调 GitLab「创建提交」API 在新分支上基于当初克隆的提交（start_sha）一次性提交，
    再建 MR。写权限令牌只在后台配置里，模型和工作区都拿不到。
  - 工作区按 应用+群 归属，闲置超过 TTL 自动清理；每个应用同时打开的数量有上限。
"""
import difflib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from core import store
from . import gitutil

TTL_HOURS = 24
MAX_OPEN_PER_APP = 5
MAX_READ_LINES = 2000
MAX_READ_BYTES = 200 * 1024
MAX_WRITE_BYTES = 1024 * 1024
MAX_SEARCH_HITS = 200
MAX_LIST = 500
MAX_DIFF_CHARS = 60000
TEST_TIMEOUT = 600
_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache", ".pytest_cache"}

_locks = {}
_locks_guard = threading.Lock()


class WorkspaceError(Exception):
    pass


def _root():
    return Path(store.DB_PATH).parent / "workspaces"


def _lock(ws_id):
    with _locks_guard:
        return _locks.setdefault(ws_id, threading.Lock())


# ---------- 仓库与工作区 ----------
def list_repos():
    """给模型看的可改仓库清单（不含任何凭证）。"""
    return [{"name": r["name"], "branch": r.get("branch") or "(默认分支)",
             "can_test": bool(r.get("test_cmd")), "can_mr": bool(r.get("write_token"))}
            for r in store.list_code_repos() if r["enabled"]]


def _repo_dir(ws):
    return Path(ws["path"]) / "repo"


def open_workspace(app_id, chat_id, repo_name, branch=None):
    repo = store.get_code_repo_by_name(repo_name)
    if not repo or not repo["enabled"]:
        raise WorkspaceError(f"没有可用的代码仓库「{repo_name}」。可用：{[r['name'] for r in list_repos()] or '（后台还没配）'}")
    opened = store.list_workspaces(status="open", app_id=app_id)
    for old in opened[MAX_OPEN_PER_APP - 1:]:          # 超出上限：关掉最久没用的
        close_workspace(old["id"], reason="超出同时打开上限")
    ws_id = uuid.uuid4().hex[:12]
    base = _root() / ws_id
    base.mkdir(parents=True, exist_ok=True)
    br = branch or repo.get("branch") or None
    try:
        commit = gitutil.clone(repo["url"], br, base / "repo", repo.get("username"), repo.get("token"))
    except gitutil.GitError as e:
        shutil.rmtree(base, ignore_errors=True)
        raise WorkspaceError(str(e))
    store.create_workspace(ws_id, app_id, chat_id, repo["id"], str(base), br or "", commit)
    ws = store.get_workspace(ws_id)
    return {"workspace": ws_id, "repo": repo["name"], "branch": br or "(默认分支)", "commit": commit[:12],
            "tree": list_dir(ws, ".")["entries"]}


def get_workspace(ws_id, app_id, chat_id):
    """取工作区并校验归属；ws_id 为空取本应用本群最近打开的。"""
    ws = store.get_workspace(ws_id) if ws_id else store.latest_workspace(app_id, chat_id)
    if not ws or ws["status"] != "open":
        raise WorkspaceError("没有打开的工作区，先用 code_open 打开一个仓库。" if not ws_id
                             else f"工作区 {ws_id} 不存在或已关闭。")
    if ws["app_id"] != app_id:
        raise WorkspaceError(f"工作区 {ws_id} 不属于本应用。")
    store.update_workspace(ws["id"], last_used=time.time())
    return ws


def close_workspace(ws_id, reason=""):
    ws = store.get_workspace(ws_id)
    if not ws:
        return False
    with _lock(ws_id):
        shutil.rmtree(ws["path"], ignore_errors=True)
        store.update_workspace(ws_id, status="closed")
    return True


def cleanup_stale(now=None):
    """调度线程调：闲置超过 TTL 的工作区关闭并删目录。"""
    now = now or time.time()
    n = 0
    for ws in store.list_workspaces(status="open"):
        if now - (ws.get("last_used") or ws.get("created") or 0) > TTL_HOURS * 3600:
            close_workspace(ws["id"], reason="闲置超时")
            n += 1
    return n


# ---------- 路径 ----------
def _resolve(ws, rel, must_exist=False, for_write=False):
    """解析为仓库内的真实路径。for_write=True 时原路径本身是符号链接也拒（否则会改到链接目标上）。"""
    root = _repo_dir(ws).resolve()
    rel = str(rel or ".").strip()
    if rel.startswith("/"):
        rel = rel.lstrip("/")
    p = (root / rel)
    if for_write and p.is_symlink():
        raise WorkspaceError(f"{rel} 是符号链接，不能通过它写入或删除。")
    try:
        real = p.resolve()
        real.relative_to(root)
    except (ValueError, RuntimeError):
        raise WorkspaceError(f"路径 {rel} 不在工作区里（不能用 ../ 或符号链接跳出仓库）。")
    parts = real.relative_to(root).parts
    if parts and parts[0] == ".git":
        raise WorkspaceError("不能读写 .git 目录。")
    if must_exist and not real.exists():
        raise WorkspaceError(f"文件不存在：{rel}")
    return real, real.relative_to(root).as_posix() if parts else "."


def _is_text(data):
    return b"\x00" not in data[:8000]


# ---------- 读 ----------
def list_dir(ws, rel=".", depth=1):
    base, rel_norm = _resolve(ws, rel, must_exist=True)
    if not base.is_dir():
        raise WorkspaceError(f"{rel} 不是目录。")
    depth = max(1, min(int(depth or 1), 4))
    root = _repo_dir(ws).resolve()
    out = []
    def walk(d, level):
        for c in sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name)):
            if c.name in _SKIP or len(out) >= MAX_LIST:
                continue
            rp = c.relative_to(root).as_posix()
            if c.is_symlink():
                out.append({"path": rp, "type": "symlink"})
            elif c.is_dir():
                out.append({"path": rp + "/", "type": "dir"})
                if level < depth:
                    walk(c, level + 1)
            else:
                out.append({"path": rp, "type": "file", "size": c.stat().st_size})
    walk(base, 1)
    return {"path": rel_norm, "entries": out, "truncated": len(out) >= MAX_LIST}


def read_file(ws, rel, start=1, end=None):
    p, rel_norm = _resolve(ws, rel, must_exist=True)
    if not p.is_file():
        raise WorkspaceError(f"{rel} 不是文件。")
    data = p.read_bytes()
    if not _is_text(data):
        return {"path": rel_norm, "binary": True, "size": len(data)}
    lines = data.decode("utf-8", "replace").splitlines()
    start = max(1, int(start or 1))
    end = min(len(lines), int(end) if end else start + MAX_READ_LINES - 1, start + MAX_READ_LINES - 1)
    body, size = [], 0
    for i in range(start, end + 1):
        ln = f"{i}\t{lines[i - 1]}"
        size += len(ln)
        if size > MAX_READ_BYTES:
            end = i - 1
            break
        body.append(ln)
    return {"path": rel_norm, "lines": f"{start}-{end}", "total_lines": len(lines),
            "content": "\n".join(body), "more": end < len(lines)}


def search(ws, pattern, glob=None, max_hits=MAX_SEARCH_HITS):
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise WorkspaceError(f"正则写法有误：{e}")
    root = _repo_dir(ws).resolve()
    hits = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in _SKIP)
        for f in sorted(files):
            p = Path(d) / f
            rel = p.relative_to(root).as_posix()
            if p.is_symlink() or (glob and not Path(rel).match(glob)):
                continue
            try:
                if p.stat().st_size > MAX_READ_BYTES * 5:
                    continue
                data = p.read_bytes()
            except OSError:
                continue
            if not _is_text(data):
                continue
            for i, ln in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(ln):
                    hits.append(f"{rel}:{i}: {ln.strip()[:300]}")
                    if len(hits) >= max_hits:
                        return {"hits": hits, "truncated": True}
    return {"hits": hits, "truncated": False}


# ---------- 写（只动工作区，记入 changed） ----------
def _mark(ws, rel, kind):
    """kind: write / delete。改回与 HEAD 一致时从 changed 里移除。"""
    changed = dict(store.get_workspace(ws["id"])["changed"])
    original = gitutil.head_blob(_repo_dir(ws), rel)
    p = _repo_dir(ws) / rel
    if kind == "delete":
        if original is None:
            changed.pop(rel, None)                         # 删的是自己新建的文件 → 等于没改
        else:
            changed[rel] = "delete"
    else:
        if original is not None and p.is_file() and p.read_bytes() == original:
            changed.pop(rel, None)
        else:
            changed[rel] = "write"
    store.update_workspace(ws["id"], changed=changed)
    return changed


def write_file(ws, rel, content):
    if content is None:
        raise WorkspaceError("content 不能为空（要清空文件请传空字符串）。")
    data = str(content).encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise WorkspaceError(f"内容超过 {MAX_WRITE_BYTES // 1024}KB，拆小一点或用 code_edit 局部修改。")
    with _lock(ws["id"]):
        p, rel_norm = _resolve(ws, rel, for_write=True)
        if rel_norm == "." or p.is_dir():
            raise WorkspaceError(f"{rel} 是目录，不能写。")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        changed = _mark(ws, rel_norm, "write")
    return {"ok": True, "path": rel_norm, "bytes": len(data), "changed_files": len(changed)}


def edit_file(ws, rel, old, new, replace_all=False):
    with _lock(ws["id"]):
        p, rel_norm = _resolve(ws, rel, must_exist=True, for_write=True)
        if not p.is_file():
            raise WorkspaceError(f"{rel} 不是普通文件。")
        data = p.read_bytes()
        if not _is_text(data):
            raise WorkspaceError(f"{rel} 是二进制文件，不能编辑。")
        text = data.decode("utf-8")
        if not old:
            raise WorkspaceError("old 不能为空。")
        n = text.count(old)
        if n == 0:
            raise WorkspaceError("old 在文件里没找到（要逐字一致，含缩进和换行），先 code_read 确认原文。")
        if n > 1 and not replace_all:
            raise WorkspaceError(f"old 在文件里出现了 {n} 次，请多带几行上下文让它唯一，或传 replace_all=true。")
        text = text.replace(old, new if new is not None else "")
        p.write_bytes(text.encode("utf-8"))
        changed = _mark(ws, rel_norm, "write")
    return {"ok": True, "path": rel_norm, "replaced": n if replace_all else 1, "changed_files": len(changed)}


def delete_file(ws, rel):
    with _lock(ws["id"]):
        p, rel_norm = _resolve(ws, rel, must_exist=True, for_write=True)
        if not p.is_file():
            raise WorkspaceError(f"{rel} 不是普通文件，只能删文件。")
        p.unlink()
        changed = _mark(ws, rel_norm, "delete")
    return {"ok": True, "path": rel_norm, "changed_files": len(changed)}


# ---------- diff ----------
def diff(ws, max_chars=MAX_DIFF_CHARS):
    ws = store.get_workspace(ws["id"])
    files, chunks = [], []
    for rel, kind in sorted(ws["changed"].items()):
        before = gitutil.head_blob(_repo_dir(ws), rel)
        p = _repo_dir(ws) / rel
        after = p.read_bytes() if kind == "write" and p.is_file() else None
        status = "deleted" if after is None else ("added" if before is None else "modified")
        a = (before or b"").decode("utf-8", "replace").splitlines(keepends=True)
        b = (after or b"").decode("utf-8", "replace").splitlines(keepends=True)
        lines = list(difflib.unified_diff(a, b, f"a/{rel}" if before is not None else "/dev/null",
                                          f"b/{rel}" if after is not None else "/dev/null"))
        add = sum(1 for x in lines if x.startswith("+") and not x.startswith("+++"))
        rm = sum(1 for x in lines if x.startswith("-") and not x.startswith("---"))
        files.append({"path": rel, "status": status, "added": add, "removed": rm})
        chunks.append("".join(x if x.endswith("\n") else x + "\n" for x in lines))
    text = "".join(chunks)
    return {"files": files, "diff": text[:max_chars], "truncated": len(text) > max_chars,
            "base_commit": (ws.get("base_commit") or "")[:12]}


# ---------- 测试 ----------
def run_tests(ws, timeout=TEST_TIMEOUT):
    repo = store.get_code_repo(ws["repo_id"]) or {}
    cmd = (repo.get("test_cmd") or "").strip()
    if not cmd:
        raise WorkspaceError("这个仓库在后台没配置测试命令，无法跑测试。")
    root = _repo_dir(ws)
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(Path(ws["path"])),
           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "CI": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    t0 = time.time()
    try:
        proc = subprocess.run(["/bin/sh", "-c", cmd], cwd=root, env=env, capture_output=True,
                              text=True, timeout=timeout)
        code, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        code = None
        out = (e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        err = f"测试超时（>{timeout}s），已终止。"
    return {"command": cmd, "exit_code": code, "passed": code == 0, "duration_s": round(time.time() - t0, 1),
            "stdout_tail": out[-6000:], "stderr_tail": err[-3000:]}


# ---------- 提 MR（GitLab API，写权限令牌只在后台） ----------
def _gitlab(repo):
    u = urlparse(repo["url"])
    if u.scheme not in ("http", "https") or not u.hostname:
        raise WorkspaceError("这个仓库不是 GitLab https 地址，不能提 MR。")
    path = u.path.strip("/")
    path = path[:-4] if path.endswith(".git") else path
    return f"{u.scheme}://{u.netloc}/api/v4/projects/{quote(path, safe='')}"


def _slug(text):
    s = re.sub(r"[^0-9a-zA-Z]+", "-", text or "").strip("-").lower()
    return s[:30] or "change"


def propose_mr(ws, title, description=""):
    repo = store.get_code_repo(ws["repo_id"]) or {}
    if not repo.get("write_token"):
        raise WorkspaceError("这个仓库在后台没配置提 MR 用的令牌，不能提 MR（diff 可以给人看后手工提交）。")
    ws = store.get_workspace(ws["id"])
    if ws.get("mr_url"):
        raise WorkspaceError(f"这个工作区已经提过 MR：{ws['mr_url']}。继续改请先 code_close 再重新打开。")
    if not ws["changed"]:
        raise WorkspaceError("工作区里没有改动，没什么可提交的。")
    title = (title or "").strip()
    if not title:
        raise WorkspaceError("请给 MR 一个标题。")
    actions = []
    for rel, kind in sorted(ws["changed"].items()):
        before = gitutil.head_blob(_repo_dir(ws), rel)
        if kind == "delete":
            actions.append({"action": "delete", "file_path": rel})
            continue
        data = (_repo_dir(ws) / rel).read_bytes()
        if not _is_text(data):
            raise WorkspaceError(f"{rel} 是二进制，不支持通过 MR 提交。")
        actions.append({"action": "update" if before is not None else "create", "file_path": rel,
                        "content": data.decode("utf-8"), "encoding": "text"})
    api = _gitlab(repo)
    headers = {"PRIVATE-TOKEN": repo["write_token"]}
    branch = f"harness/{ws['id'][:8]}-{_slug(title)}"
    target = ws.get("base_branch") or repo.get("branch") or "main"
    body = (description or "").strip() + f"\n\n---\n由 feishu-agent 数字员工在工作区 `{ws['id']}` 中生成，" \
                                          f"基于 `{(ws.get('base_commit') or '')[:12]}`。"

    def call(method, url, payload):
        try:
            r = requests.request(method, url, json=payload, headers=headers, timeout=30)
        except requests.RequestException as e:
            raise WorkspaceError(f"连不上 GitLab：{type(e).__name__}")
        if r.status_code >= 300:
            txt = (r.text or "")[:400].replace(repo["write_token"], "***")
            raise WorkspaceError(f"GitLab 返回 {r.status_code}：{txt}")
        return r.json()

    with _lock(ws["id"]):
        call("POST", f"{api}/repository/commits", {
            "branch": branch, "start_sha": ws["base_commit"], "commit_message": title, "actions": actions})
        mr = call("POST", f"{api}/merge_requests", {
            "source_branch": branch, "target_branch": target, "title": title, "description": body,
            "remove_source_branch": True})
        store.update_workspace(ws["id"], mr_url=mr.get("web_url") or "", mr_branch=branch)
    return {"ok": True, "mr_url": mr.get("web_url"), "mr_iid": mr.get("iid"), "branch": branch,
            "target_branch": target, "files": len(actions)}
