"""代码工作区：让数字员工在一次性工作区里改代码、跑测试、出 diff，最后由确定性代码提 MR。

边界：
  - 模型只能动工作区里的文件（路径解析后必须落在仓库目录内，不能碰 .git，符号链接跳出一律拒）。
  - diff 只看「模型通过工具改过的文件」（workspaces.changed），和 HEAD 比；跑测试生成的产物不进 MR。
  - 跑测试只能跑后台配置的命令，输入复制到无网络的独立 Docker 沙箱；不可用时拒绝，绝不回退主机 shell。
  - 审批固定工作区和完整输入快照；测试证明与 MR 绑定同一快照，文件变化会使旧审批/测试失效。
  - 提 MR 不用 git push：调 GitLab「创建提交」API 在新分支上基于当初克隆的提交（start_sha）一次性提交，
    再建 MR。写权限令牌只在后台配置里，模型和工作区都拿不到。
  - 工作区按 应用+群 归属，闲置超过 TTL 自动清理；每个应用同时打开的数量有上限。
"""
import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
import yaml

from core import store
from . import gitutil, sandbox

TTL_HOURS = 24
MAX_OPEN_PER_APP = 5
MAX_READ_LINES = 2000
MAX_READ_BYTES = 200 * 1024
MAX_WRITE_BYTES = 1024 * 1024
MAX_SEARCH_HITS = 200
MAX_LIST = 500
MAX_DIFF_CHARS = 60000
TEST_TIMEOUT = 600
MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024
MAX_SNAPSHOT_FILES = 20000
_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache", ".pytest_cache"}

_locks = {}
_locks_guard = threading.Lock()


class WorkspaceError(Exception):
    pass


def _root():
    return Path(store.DB_PATH).parent / "workspaces"


def _lock(ws_id):
    with _locks_guard:
        return _locks.setdefault(ws_id, threading.RLock())


def _task():
    from . import tasks
    return tasks.current()


def _active():
    from . import tasks
    tasks.check_active()


def _meta(ws_id):
    with store._lock:
        store._conn.execute("CREATE TABLE IF NOT EXISTS workspace_safety "
                            "(workspace_id TEXT PRIMARY KEY, task_id TEXT, test_result TEXT, mr_state TEXT, "
                            "mr_snapshot TEXT, commit_sha TEXT, validation_result TEXT)")
        columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(workspace_safety)")}
        if "validation_result" not in columns:
            store._conn.execute("ALTER TABLE workspace_safety ADD COLUMN validation_result TEXT")
        store._conn.commit()
    row = store._conn.execute("SELECT * FROM workspace_safety WHERE workspace_id=?", (ws_id,)).fetchone()
    return dict(row) if row else {}


def _save_meta(ws_id, **values):
    _meta(ws_id)
    allowed = {"task_id", "test_result", "mr_state", "mr_snapshot", "commit_sha", "validation_result"}
    if not values or set(values) - allowed:
        raise ValueError("invalid workspace metadata")
    with store._lock:
        store._conn.execute("INSERT OR IGNORE INTO workspace_safety(workspace_id) VALUES(?)", (ws_id,))
        store._conn.execute("UPDATE workspace_safety SET " + ",".join(k + "=?" for k in values) +
                            " WHERE workspace_id=?", [*values.values(), ws_id])
        store._conn.commit()


def _ensure_mutable(ws):
    _active()
    fresh = store.get_workspace(ws["id"])
    if not fresh or fresh["status"] != "open":
        raise WorkspaceError("工作区已关闭。")
    if _meta(ws["id"]).get("mr_state"):
        raise WorkspaceError("工作区已经开始提交 MR，不能再修改；请打开新工作区。")


# ---------- 仓库与工作区 ----------
def list_repos():
    """给模型看的可改仓库清单（不含任何凭证）。"""
    return [{"name": r["name"], "branch": r.get("branch") or "(默认分支)",
             "can_test": bool(r.get("test_cmd")) and bool(sandbox.image_name()),
             "test_backend": "docker", "validation": r.get("validation_kind") or "",
             "can_mr": bool(r.get("write_token"))}
            for r in store.list_code_repos() if r["enabled"]]


def _repo_dir(ws):
    return Path(ws["path"]) / "repo"


def open_workspace(app_id, chat_id, repo_name, branch=None):
    _active()
    repo = store.get_code_repo_by_name(repo_name)
    if not repo or not repo["enabled"]:
        raise WorkspaceError(f"没有可用的代码仓库「{repo_name}」。可用：{[r['name'] for r in list_repos()] or '（后台还没配）'}")
    opened = store.list_workspaces(status="open", app_id=app_id)
    if len(opened) >= MAX_OPEN_PER_APP:
        raise WorkspaceError("打开的工作区已达上限，请先关闭已交付任务的工作区。")
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
    task = _task()
    _save_meta(ws_id, task_id=task["id"] if task else None)
    ws = store.get_workspace(ws_id)
    return {"workspace": ws_id, "repo": repo["name"], "branch": br or "(默认分支)", "commit": commit[:12],
            "tree": list_dir(ws, ".")["entries"]}


def get_workspace(ws_id, app_id, chat_id):
    """取工作区并校验归属；ws_id 为空取本应用本群最近打开的。"""
    task = _task()
    task_id = task["id"] if task else None
    if ws_id:
        ws = store.get_workspace(ws_id)
    else:
        ws = next((w for w in store.list_workspaces(status="open", app_id=app_id)
                   if w["chat_id"] == chat_id and _meta(w["id"]).get("task_id") == task_id), None)
    if not ws or ws["status"] != "open":
        raise WorkspaceError("没有打开的工作区，先用 code_open 打开一个仓库。" if not ws_id
                             else f"工作区 {ws_id} 不存在或已关闭。")
    if ws["app_id"] != app_id or ws["chat_id"] != chat_id:
        raise WorkspaceError("工作区不属于本应用和会话。")
    if _meta(ws["id"]).get("task_id") != task_id:
        raise WorkspaceError("工作区不属于当前任务。")
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


def _expand_braces(pat):
    """{a,b} 展开：'**/*.{yaml,yml}' → ['**/*.yaml', '**/*.yml']（支持多组、不支持嵌套）。"""
    m = re.search(r"\{([^{}]*)\}", pat)
    if not m:
        return [pat]
    out = []
    for alt in m.group(1).split(","):
        out += _expand_braces(pat[:m.start()] + alt + pat[m.end():])
    return out


def _glob_regex(pat):
    """glob → 正则：** 跨任意层目录，* 和 ? 不跨 /。"""
    i, out = 0, []
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif pat.startswith("**", i):
            out.append(".*"); i += 2
        elif pat[i] == "*":
            out.append("[^/]*"); i += 1
        elif pat[i] == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(pat[i])); i += 1
    return re.compile("".join(out) + r"\Z")


def glob_matcher(glob):
    """返回 rel_path -> bool。不含 / 的模式（如 *.yaml）按文件名在任意深度匹配（同 ripgrep/git 习惯）。"""
    pats = [p[2:] if p.startswith("./") else p for p in (x.strip() for x in _expand_braces(glob.strip())) if p]
    rxs = [(_glob_regex(p), "/" not in p) for p in pats]
    return lambda rel: any(rx.match(rel.rsplit("/", 1)[-1] if base_only else rel) for rx, base_only in rxs)


def search(ws, pattern, glob=None, max_hits=MAX_SEARCH_HITS):
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise WorkspaceError(f"正则写法有误：{e}")
    match = glob_matcher(glob) if glob else None
    root = _repo_dir(ws).resolve()
    hits, scanned, in_glob = [], 0, 0
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in _SKIP)
        for f in sorted(files):
            p = Path(d) / f
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                continue
            scanned += 1
            if match and not match(rel):
                continue
            in_glob += 1
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
                        return {"hits": hits, "truncated": True, "files_scanned": scanned,
                                "note": f"已达 {max_hits} 条上限，请用更具体的 pattern 或 glob 缩小范围"}
    out = {"hits": hits, "truncated": False, "files_scanned": scanned, "files_matched_glob": in_glob}
    if match and in_glob == 0:
        out["note"] = (f"glob「{glob}」一个文件都没匹配上（共扫描 {scanned} 个文件）——是过滤条件的问题，不代表仓库里没有。"
                       "可去掉 glob 重试，或用 *.yaml、apps/**/*.yaml 这类写法。")
    elif not hits:
        out["note"] = f"在 {in_glob} 个文件里没有匹配；可以换个写法（如去掉锚点、放宽正则）再搜。"
    return out


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
        _ensure_mutable(ws)
        p, rel_norm = _resolve(ws, rel, for_write=True)
        if rel_norm == "." or p.is_dir():
            raise WorkspaceError(f"{rel} 是目录，不能写。")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        changed = _mark(ws, rel_norm, "write")
    return {"ok": True, "path": rel_norm, "bytes": len(data), "changed_files": len(changed)}


def edit_file(ws, rel, old, new, replace_all=False):
    with _lock(ws["id"]):
        _ensure_mutable(ws)
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
        _ensure_mutable(ws)
        p, rel_norm = _resolve(ws, rel, must_exist=True, for_write=True)
        if not p.is_file():
            raise WorkspaceError(f"{rel} 不是普通文件，只能删文件。")
        p.unlink()
        changed = _mark(ws, rel_norm, "delete")
    return {"ok": True, "path": rel_norm, "changed_files": len(changed)}


# ---------- 固定目标与快照 ----------
def _snapshot(ws, copy_to=None):
    """完整输入摘要，包含未修改文件和测试配置；拷贝与摘要使用同一批读取的字节。"""
    fresh = store.get_workspace(ws["id"])
    repo = store.get_code_repo(ws["repo_id"]) or {}
    if not fresh or fresh["status"] != "open" or not repo.get("enabled"):
        raise WorkspaceError("工作区或代码仓库已停用。")
    root = _repo_dir(ws).resolve()
    digest = hashlib.sha256()
    context = {"workspace": ws["id"], "repo_id": ws["repo_id"], "repo_url": repo.get("url"),
               "base_commit": fresh["base_commit"], "base_branch": fresh["base_branch"],
               "changed": fresh["changed"], "command": repo.get("test_cmd") or "",
               "validation": repo.get("validation_kind") or "",
               "sandbox_image": sandbox.resolve_image() if repo.get("test_cmd") else None}
    digest.update(json.dumps(context, sort_keys=True, ensure_ascii=False).encode())
    count, size = 0, 0
    if copy_to:
        copy_to = Path(copy_to)
        copy_to.mkdir(parents=True, exist_ok=True)
        copy_to.chmod(0o755)
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(directory) == root and d == ".git"))
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise WorkspaceError("测试/提交快照不接受符号链接目录。")
        for name in sorted(files):
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            if rel == ".git":
                continue
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode) or path.is_symlink():
                raise WorkspaceError(f"快照包含非普通文件：{rel}")
            count += 1
            size += path.stat().st_size
            if count > MAX_SNAPSHOT_FILES or size > MAX_SNAPSHOT_BYTES:
                raise WorkspaceError("工作区超过快照大小限制，请移除无关依赖/构建产物。")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                data = handle.read(MAX_SNAPSHOT_BYTES + 1)
            if len(data) > MAX_SNAPSHOT_BYTES:
                raise WorkspaceError("快照文件过大。")
            executable = bool(mode & 0o111)
            digest.update(json.dumps([rel, executable, len(data)], ensure_ascii=False).encode())
            digest.update(data)
            if copy_to:
                dest = copy_to / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                dest.chmod(0o755 if executable else 0o644)
    return digest.hexdigest()


def prepare_action(name, args, ctx):
    """先固定 workspace 默认值，再把审批绑定到完整代码与测试配置。"""
    if name not in ("code_test", "code_propose_mr"):
        return dict(args)
    ws = get_workspace(args.get("workspace"), ctx["app_id"], ctx["chat_id"])
    with _lock(ws["id"]):
        prepared = {k: v for k, v in args.items() if not k.startswith("_")}
        prepared["workspace"] = ws["id"]
        prepared["_snapshot"] = _snapshot(ws)
        prepared["_test_command"] = (store.get_code_repo(ws["repo_id"]) or {}).get("test_cmd") or ""
        return prepared


def validate_action(name, args, ctx):
    if name not in ("code_test", "code_propose_mr"):
        return
    ws = get_workspace(args.get("workspace"), ctx["app_id"], ctx["chat_id"])
    with _lock(ws["id"]):
        if not args.get("_snapshot") or _snapshot(ws) != args["_snapshot"]:
            raise WorkspaceError("审批后的代码、目标或测试配置已变化，请重新准备并审批本次操作。")


def _changed_bytes(ws):
    """提交路径重新检查，不能通过测试产物的符号链接读取控制面文件。"""
    fresh = store.get_workspace(ws["id"])
    for rel, kind in sorted(fresh["changed"].items()):
        path, norm = _resolve(ws, rel, must_exist=kind == "write", for_write=True)
        if norm != rel or kind not in ("write", "delete") or norm == ".":
            raise WorkspaceError("工作区变更路径无效。")
        data = None
        if kind == "write":
            if not path.is_file() or path.stat().st_size > MAX_WRITE_BYTES:
                raise WorkspaceError(f"变更文件不是受支持的普通文本文件：{rel}")
            data = path.read_bytes()
        yield rel, kind, data


# ---------- diff ----------
def diff(ws, max_chars=MAX_DIFF_CHARS):
    ws = store.get_workspace(ws["id"])
    files, chunks = [], []
    for rel, kind, after in _changed_bytes(ws):
        before = gitutil.head_blob(_repo_dir(ws), rel)
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
def run_tests(ws, timeout=TEST_TIMEOUT, expected_snapshot=None):
    repo = store.get_code_repo(ws["repo_id"]) or {}
    cmd = (repo.get("test_cmd") or "").strip()
    if not cmd:
        raise WorkspaceError("这个仓库在后台没配置测试命令，无法跑测试。")
    with _lock(ws["id"]):
        _ensure_mutable(ws)
        with tempfile.TemporaryDirectory(prefix="feishu-test-input-") as temp:
            snapshot = _snapshot(ws, temp)
            if expected_snapshot and snapshot != expected_snapshot:
                raise WorkspaceError("代码/测试配置已变化，本次审批已失效。")
            try:
                result = sandbox.run(temp, cmd, timeout=timeout, check_active=_active)
            except sandbox.SandboxUncertain as e:
                return {"error": str(e), "outcome_unknown": True}
            except sandbox.SandboxError as e:
                raise WorkspaceError(str(e)) from e
            _active()
            if _snapshot(ws) != snapshot:
                raise WorkspaceError("测试期间工作区发生变化，测试证明已失效。")
            result["snapshot"] = snapshot
            _save_meta(ws["id"], test_result=json.dumps(result, ensure_ascii=False))
            return result


def validate_manifests(ws):
    """静态校验 GitOps 工作区的全部改动；不执行仓库代码，也不返回 Secret 载荷。"""
    with _lock(ws["id"]):
        _ensure_mutable(ws)
        repo = store.get_code_repo(ws["repo_id"]) or {}
        if repo.get("validation_kind") != "gitops":
            raise WorkspaceError("该仓库未配置 GitOps 静态校验。")
        snapshot = _snapshot(ws)
        changes = list(_changed_bytes(ws))
        if not changes:
            raise WorkspaceError("工作区没有待校验的改动。")
        errors, files = [], []
        for rel, kind, data in changes:
            files.append(rel)
            if not rel.endswith((".yaml", ".yml", ".json")):
                errors.append(f"{rel}: GitOps 修复仅接受 YAML/JSON 清单文件")
                continue
            if kind == "delete":
                continue
            try:
                docs = list(yaml.safe_load_all(data.decode("utf-8")))
                for index, doc in enumerate(docs, 1):
                    if isinstance(doc, dict) and doc.get("kind") == "Secret" and any(
                            bool(doc.get(field)) for field in ("data", "stringData")):
                        errors.append(f"{rel} 第 {index} 段: 原生 Secret 含明文数据，请改用 SealedSecret")
            except (UnicodeError, yaml.YAMLError):
                errors.append(f"{rel}: YAML/JSON 语法无效")
        result = {"passed": not errors, "files": files, "errors": errors[:20],
                  "snapshot": snapshot, "kind": "gitops", "note": "仅校验改动文件语法与原生 Secret；发布仍需 CI 和 ArgoCD 验证"}
        _save_meta(ws["id"], validation_result=json.dumps(result, ensure_ascii=False))
        return result


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


def propose_mr(ws, title, description="", expected_snapshot=None):
    with _lock(ws["id"]):
        return _propose_mr_locked(ws, title, description, expected_snapshot)


def _propose_mr_locked(ws, title, description, expected_snapshot):
    _active()
    repo = store.get_code_repo(ws["repo_id"]) or {}
    if not repo.get("write_token"):
        raise WorkspaceError("这个仓库在后台没配置提 MR 用的令牌，不能提 MR（diff 可以给人看后手工提交）。")
    ws = store.get_workspace(ws["id"])
    if ws.get("mr_url"):
        return {"ok": True, "mr_url": ws["mr_url"], "branch": ws.get("mr_branch"), "reused": True}
    snapshot = _snapshot(ws)
    if expected_snapshot and expected_snapshot != snapshot:
        raise WorkspaceError("代码/目标/测试配置已变化，本次审批已失效。")
    meta = _meta(ws["id"])
    if meta.get("mr_snapshot") and meta["mr_snapshot"] != snapshot:
        raise WorkspaceError("此前提交快照与当前工作区不一致，请核对已有远端提交。")
    if meta.get("mr_state") in ("committing", "commit_unknown"):
        return {"error": "上次创建提交的结果不确定，请核对远端分支后再处理，禁止自动重提。",
                "outcome_unknown": True, "branch": ws.get("mr_branch")}
    if repo.get("test_cmd"):
        report = json.loads(meta.get("test_result") or "{}")
        if not report.get("passed") or report.get("snapshot") != snapshot:
            raise WorkspaceError("必须先对当前代码快照运行并通过沙箱测试，才能提 MR。")
    if repo.get("validation_kind") == "gitops":
        report = json.loads(meta.get("validation_result") or "{}")
        if not report.get("passed") or report.get("snapshot") != snapshot:
            raise WorkspaceError("必须先对当前代码快照通过 GitOps 静态校验，才能提 MR。")
    if not ws["changed"]:
        raise WorkspaceError("工作区里没有改动，没什么可提交的。")
    title = (title or "").strip()
    if not title:
        raise WorkspaceError("请给 MR 一个标题。")
    actions = []
    for rel, kind, data in _changed_bytes(ws):
        before = gitutil.head_blob(_repo_dir(ws), rel)
        if kind == "delete":
            actions.append({"action": "delete", "file_path": rel})
            continue
        if not _is_text(data):
            raise WorkspaceError(f"{rel} 是二进制，不支持通过 MR 提交。")
        actions.append({"action": "update" if before is not None else "create", "file_path": rel,
                        "content": data.decode("utf-8"), "encoding": "text"})
    api = _gitlab(repo)
    headers = {"PRIVATE-TOKEN": repo["write_token"]}
    branch = ws.get("mr_branch") or f"harness/{ws['id']}-{snapshot[:12]}"
    target = ws.get("base_branch") or repo.get("branch") or "main"
    body = (description or "").strip() + f"\n\n---\n由 feishu-agent 数字员工在工作区 `{ws['id']}` 中生成，" \
                                          f"基于 `{(ws.get('base_commit') or '')[:12]}`。"

    def call(method, url, payload=None):
        _active()
        try:
            r = requests.request(method, url, json=payload, headers=headers, timeout=30)
        except requests.RequestException as e:
            raise WorkspaceError(f"连不上 GitLab：{type(e).__name__}")
        if r.status_code >= 300:
            txt = (r.text or "")[:400].replace(repo["write_token"], "***")
            raise WorkspaceError(f"GitLab 返回 {r.status_code}：{txt}")
        try:
            return r.json()
        except ValueError as e:
            raise WorkspaceError("GitLab 返回无效响应，需核对远端状态。") from e

    store.update_workspace(ws["id"], mr_branch=branch)
    if not meta.get("commit_sha"):
        _save_meta(ws["id"], mr_state="committing", mr_snapshot=snapshot)
        try:
            commit = call("POST", f"{api}/repository/commits", {
                "branch": branch, "start_sha": ws["base_commit"],
                "commit_message": title + f"\n\nHarness-Snapshot: {snapshot}", "actions": actions})
            sha = commit.get("id")
            if not sha:
                raise WorkspaceError("GitLab 未返回提交 ID。")
            _save_meta(ws["id"], mr_state="committed", commit_sha=sha)
        except WorkspaceError as e:
            _save_meta(ws["id"], mr_state="commit_unknown")
            return {"error": str(e), "outcome_unknown": True, "branch": branch}
    try:
        # 上次建 MR 丢响应时，先核对同一确定性分支；绝不重复提交代码。
        existing = call("GET", f"{api}/merge_requests?source_branch={quote(branch, safe='')}&state=all")
        matches = [m for m in existing if m.get("source_branch") == branch and m.get("target_branch") == target]
        if matches:
            mr = matches[0]
        else:
            if meta.get("mr_state") in ("mr_creating", "mr_unknown"):
                return {"error": "此前创建 MR 的结果尚未核实，请人工核对；不会再次发送创建请求。",
                        "outcome_unknown": True, "branch": branch}
            _save_meta(ws["id"], mr_state="mr_creating")
            mr = call("POST", f"{api}/merge_requests", {
                "source_branch": branch, "target_branch": target, "title": title, "description": body,
                "remove_source_branch": True})
        if not mr.get("web_url"):
            raise WorkspaceError("GitLab 未返回 MR 地址。")
        store.update_workspace(ws["id"], mr_url=mr["web_url"], mr_branch=branch)
        _save_meta(ws["id"], mr_state="completed")
    except WorkspaceError as e:
        _save_meta(ws["id"], mr_state="mr_unknown")
        return {"error": str(e), "outcome_unknown": True, "branch": branch}
    return {"ok": True, "mr_url": mr.get("web_url"), "mr_iid": mr.get("iid"), "branch": branch,
            "target_branch": target, "files": len(actions)}
