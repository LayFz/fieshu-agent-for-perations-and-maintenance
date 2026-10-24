"""业务源码仓库（只读）：从线上镜像找到对应的 GitLab 项目和提交，读取依赖相关文件，定位要改的行。

边界：
  - 只读，用后台配置的 GitLab 令牌（建议 Reporter + read_api 的专用账号），并受应用资源权限 gitlab_projects 限制。
  - 只读依赖与构建清单（Dockerfile、requirements、package.json、go.mod、各类锁文件…），不读业务代码；
    返回给模型前对疑似密钥打码。
  - 读的是线上镜像对应的那次提交（镜像 tag 即 8 位提交号），不是默认分支的最新代码。
"""
import json
import re
from urllib.parse import quote

from core import components, store
from . import gitlab

DEP_FILES = re.compile(r"(^|/)(Dockerfile[^/]*|[^/]+\.dockerfile|requirements[^/]*\.txt|pyproject\.toml|poetry\.lock|"
                       r"Pipfile(\.lock)?|setup\.cfg|package\.json|package-lock\.json|pnpm-lock\.yaml|yarn\.lock|"
                       r"go\.mod|go\.sum|composer\.(json|lock)|Gemfile(\.lock)?|Cargo\.(toml|lock)|pom\.xml|"
                       r"build\.gradle(\.kts)?)$", re.I)
_SKIP_DIRS = re.compile(r"(^|/)(node_modules|vendor|\.git|dist|build|\.venv|venv|test|tests|docs|examples?)/", re.I)
_MAX_FILES = 30
_SECRET = [
    (re.compile(r"(://[^:/@\s]+:)[^@\s]+@"), r"\1***@"),
    (re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)[\w-]*\s*[:=]\s*)"
                r"(\"[^\"]*\"|'[^']*'|\S+)"), r"\1***"),
    (re.compile(r"glpat-[\w-]{10,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[bp]-[\w-]{10,}"), "***"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "***私钥已隐藏***"),
]


def redact(text):
    for pattern, repl in _SECRET:
        text = pattern.sub(repl, text)
    return text


def _commit_exists(project, sha):
    data, err = gitlab._get(f"/projects/{gitlab._pid(project)}/repository/commits/{quote(sha, safe='')}", project=project)
    return (data or {}).get("id") if not err and isinstance(data, dict) else None


def resolve(image, allowed=None, own_registries=()):
    """线上镜像 → {project, commit, how}。镜像 tag 是提交号时，按名字找候选项目并用「该项目有这个提交」确认。
    后台 kv image_repo_map {"harbor项目/仓库": "gitlab项目路径"} 可手动指定。"""
    registry, path, tag, _ = components.parse_image(image)
    if not registry or not any(registry == r or registry.endswith("." + r) for r in own_registries):
        return {"error": "不是自家 Harbor 的镜像"}
    sha = tag if re.fullmatch(r"[0-9a-f]{7,40}", tag or "") else None
    try:
        manual = json.loads(store.get_setting("image_repo_map") or "{}")
    except ValueError:
        manual = {}
    try:
        cache = json.loads(store.get_setting("image_repo_cache") or "{}")
    except ValueError:
        cache = {}
    candidates = []
    if isinstance(manual, dict) and manual.get(path):
        candidates.append((manual[path], "后台手动指定"))
    if isinstance(cache, dict) and cache.get(path):
        candidates.append((cache[path], "此前已确认"))
    search_error = None
    for term in dict.fromkeys([path.rsplit("/", 1)[-1], path.split("/", 1)[0]]):
        found = gitlab.projects(search=term, limit=20)
        if isinstance(found, dict) and found.get("error"):
            search_error = found["error"]
        for p in (found.get("projects") or []) if isinstance(found, dict) else []:
            candidates.append((p["path"], f"按名称「{term}」搜索"))
    if not candidates and search_error:
        return {"error": f"无法查询业务仓库：{search_error}（需要配置能读取业务项目的 GitLab 只读令牌）"}
    seen = set()
    for project, how in candidates:
        if project in seen or (allowed is not None and not allowed(project)):
            continue
        seen.add(project)
        if not sha:
            if how == "后台手动指定":
                return {"project": project, "commit": None, "how": how + "（镜像 tag 不是提交号，按默认分支读取）"}
            continue
        full = _commit_exists(project, sha)
        if full:
            if how.startswith("按名称"):
                cache[path] = project
                store.set_setting("image_repo_cache", json.dumps(cache, ensure_ascii=False))
            return {"project": project, "commit": full, "how": f"{how}，并确认该项目包含提交 {sha}"}
        if len(seen) >= 10:
            break
    if allowed is not None and not any(allowed(p) for p, _h in candidates):
        return {"error": "找到的候选项目都不在应用的 gitlab_projects 授权里" if candidates else
                         "GitLab 上没搜到候选项目（令牌账号可能未加入该项目）"}
    return {"error": f"没有项目包含提交 {sha}" if sha else "镜像 tag 不是提交号，且没有手动指定仓库",
            "candidates": [p for p, _h in candidates][:5]}


def dependency_files(project, ref):
    """列出提交里的依赖/构建清单（跳过 node_modules、测试、文档等目录）。"""
    files, page = [], 1
    while page <= 5:
        data, err = gitlab._get(f"/projects/{gitlab._pid(project)}/repository/tree",
                                {"ref": ref or None, "recursive": "true", "per_page": 100, "page": page}, project=project)
        if err or not isinstance(data, list):
            break
        for e in data:
            p = e.get("path") or ""
            if e.get("type") == "blob" and DEP_FILES.search(p) and not _SKIP_DIRS.search(p) and p.count("/") <= 3:
                files.append(p)
        if len(data) < 100:
            break
        page += 1
    return files[:_MAX_FILES]


def read_files(project, ref, paths):
    out = {}
    for p in paths:
        r = gitlab.read(project, p, ref)
        if r.get("content") is not None:
            out[p] = redact(r["content"])
    return out


_NORMALIZE = re.compile(r"[-_.]+")


def _norm(name):
    return _NORMALIZE.sub("-", (name or "").lower())


def locate(package, files):
    """在依赖清单里找提到这个包的行：[{path, line, text}]。按包名规范化后做词边界匹配（Pillow ≈ pillow）。"""
    target = _norm(package.split("/")[-1] if package.startswith(("golang.org", "github.com")) else package)
    hits = []
    for path, text in files.items():
        if re.search(r"(^|/)Dockerfile|\.dockerfile$", path, re.I):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            low = _norm(line)
            # 规范化后包名里的分隔符都是「-」：边界也不能是「-」，否则 django 会命中 django-redis
            if re.search(r"(?<![a-z0-9-])" + re.escape(target) + r"(?![a-z0-9-])", low) or \
                    (package.startswith(("golang.org", "github.com")) and package in line):
                hits.append({"path": path, "line": i, "text": line.strip()[:160]})
                if len(hits) >= 6:
                    return hits
    return hits


def base_images(files):
    """Dockerfile 里的 FROM 行：[{path, line, image}]（多阶段构建会有多行）。"""
    out = []
    for path, text in files.items():
        if not re.search(r"(^|/)Dockerfile|\.dockerfile$", path, re.I):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            m = re.match(r"\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", line, re.I)
            if m and m.group(1).lower() != "scratch":
                out.append({"path": path, "line": i, "image": m.group(1)})
    return out
