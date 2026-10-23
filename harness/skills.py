"""技能从 Git 同步：架构知识只维护一份，直接读公司 GitLab skill marketplace 里的 SKILL.md，和 Claude Code 共用。

两种技能源（skill_sources 表）：
  marketplace  中央索引仓（.claude-plugin/marketplace.json）→ 逐个 clone 其中的能力仓
  repo         单个能力仓（可选 subdir）

能力仓里的技能：仓根（或 subdir）有 SKILL.md = 一个技能；skills/*/SKILL.md = 多个技能。
技能目录下的文本文件（references/、脚本…）一并收进 skills.files，read_skill(name, file=…) 可读。

安全与容错：
  - git 走 dulwich（纯 Python，镜像里不用装 git，避开构建机 apt 卡死）
  - 索引里的 ssh 地址按 rewrite 规则改写成 https，用只读 Deploy Token 认证
  - 令牌只发给和技能源同一主机的地址；索引里写了别的主机一律拒绝（索引是仓库内容，不能信它把令牌带走）
  - 某个能力仓拉取失败：它的技能保留上一版；只有拉取成功且确认技能已不存在才删除
  - 同名冲突（和手工技能或别的来源撞名）跳过并记录，不覆盖
"""
import io
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml

from core import store

_TEXT_EXT = {".md", ".markdown", ".txt", ".yaml", ".yml", ".json", ".sh", ".py", ".toml", ".ini", ".conf", ".sql"}
_MAX_FILE = 100 * 1024           # 单个附属文件上限
_MAX_FILES_TOTAL = 512 * 1024    # 每个技能附属文件总量上限
_SKIP_DIRS = {".git", ".claude-plugin", "node_modules", "__pycache__", "eval", "evals"}

_sync_lock = threading.Lock()    # 同一时间只跑一次同步（调度与手动触发互斥）


class SyncError(Exception):
    pass


# ---------- git ----------
def rewrite_url(url, rules):
    """按 [[from, to], ...] 做前缀改写（同 git insteadOf），取第一条命中的。"""
    for rule in rules or []:
        if isinstance(rule, (list, tuple)) and len(rule) == 2 and rule[0] and url.startswith(rule[0]):
            return rule[1] + url[len(rule[0]):]
    return url


def _host(url):
    p = urlparse(url)
    return (p.hostname or "").lower() if p.scheme else ""


def _inside(base, path):
    """path 解析后必须落在 base 里（防索引/子路径用 ../ 跳出克隆目录）。"""
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except ValueError:
        return False


def _clone(url, branch, dest, username="", token=""):
    """浅克隆到 dest，返回 HEAD 提交号。本地路径（测试/开发用）走完整克隆。"""
    from dulwich import porcelain
    kw = {"errstream": io.BytesIO(), "checkout": True}
    remote = url.startswith(("http://", "https://"))
    if remote:
        kw["depth"] = 1
        if token:
            kw.update(username=username or "deploy-token", password=token)
    elif url.startswith("ssh://") or url.startswith("git@"):
        raise SyncError(f"不支持 ssh 地址（镜像里没有 ssh 客户端）：{url}。请在技能源里配置改写规则把它转成 https。")
    if branch:
        kw["branch"] = branch
    try:
        repo = porcelain.clone(url, str(dest), **kw)
    except Exception as e:
        msg = str(e)
        if token:
            msg = msg.replace(token, "***")
        raise SyncError(f"克隆 {url} 失败：{type(e).__name__}: {msg[:300]}")
    try:
        return repo.head().decode()
    finally:
        repo.close()


# ---------- 解析 ----------
def parse_skill_md(text):
    """拆出 frontmatter（name/description）和正文。没有 frontmatter 时 name/description 为空。"""
    meta, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            try:
                meta = yaml.safe_load(text[3:end]) or {}
            except Exception:
                meta = {}
            body = text[end + 4:].lstrip("\n")
    if not isinstance(meta, dict):
        meta = {}
    return str(meta.get("name") or "").strip(), str(meta.get("description") or "").strip(), body


def _collect_files(skill_dir, exclude):
    """技能目录下的文本附属文件。超限的跳过并在返回的 skipped 里记一笔。"""
    files, skipped, total = {}, [], 0
    for root, dirs, names in os.walk(skill_dir):          # 默认不跟随目录符号链接
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for n in sorted(names):
            p = Path(root) / n
            rel = p.relative_to(skill_dir).as_posix()
            if p == exclude or p.suffix.lower() not in _TEXT_EXT or n.startswith("."):
                continue
            if p.is_symlink():                              # 仓库里的符号链接可能指向容器内任意文件，一律不读
                skipped.append(rel)
                continue
            size = p.stat().st_size
            if size > _MAX_FILE or total + size > _MAX_FILES_TOTAL:
                skipped.append(rel)
                continue
            try:
                files[rel] = p.read_text(encoding="utf-8")
                total += size
            except UnicodeDecodeError:
                skipped.append(rel)
    return files, skipped


def discover(plugin_root):
    """在能力仓根下找技能：根目录 SKILL.md，或 skills/*/SKILL.md。返回 [{path,name,description,content,files}]。"""
    root = Path(plugin_root)
    found = []
    candidates = []

    def ok(md):
        return md.is_file() and not md.is_symlink() and not md.parent.is_symlink()

    if ok(root / "SKILL.md"):
        candidates.append(root)
    skills_dir = root / "skills"
    if skills_dir.is_dir() and not skills_dir.is_symlink():
        candidates += sorted(d for d in skills_dir.iterdir() if ok(d / "SKILL.md"))
    for d in candidates:
        name, desc, body = parse_skill_md((d / "SKILL.md").read_text(encoding="utf-8"))
        files, skipped = _collect_files(d, d / "SKILL.md")
        # 技能在仓根时，skills/ 下的子技能单独成条，别再当附属文件收一遍
        if d == root:
            files = {k: v for k, v in files.items() if not k.startswith("skills/")}
        found.append({"path": d.relative_to(root).as_posix() or ".", "name": name or d.name,
                      "description": desc, "content": body, "files": files, "skipped": skipped})
    return found


def _plugin_name(plugin_root, fallback):
    pj = Path(plugin_root) / ".claude-plugin" / "plugin.json"
    try:
        return json.loads(pj.read_text(encoding="utf-8")).get("name") or fallback
    except Exception:
        return fallback


# ---------- 同步 ----------
def _plugin_entries(src, workdir):
    """返回 [(plugin 名, 克隆地址, 仓内子路径)]；marketplace 类先克隆索引仓解析 marketplace.json。"""
    url = rewrite_url(src["url"], src.get("rewrite"))
    if src.get("kind") == "repo":
        return [(None, url, (src.get("subdir") or "").strip("/"))], None
    idx_dir = Path(workdir) / "_index"
    rev = _clone(url, src.get("branch"), idx_dir, src.get("username"), src.get("token"))
    mj = idx_dir / ".claude-plugin" / "marketplace.json"
    if not mj.is_file():
        raise SyncError("索引仓里没有 .claude-plugin/marketplace.json")
    try:
        index = json.loads(mj.read_text(encoding="utf-8"))
    except Exception as e:
        raise SyncError(f"marketplace.json 解析失败：{e}")
    entries = []
    for p in index.get("plugins") or []:
        name, s = p.get("name"), p.get("source")
        if isinstance(s, str):                                  # 索引仓内的相对路径，如 ./plugins/x
            entries.append((name, "@index", s[2:] if s.startswith("./") else s.strip("/")))
        elif isinstance(s, dict) and s.get("source") in ("url", "git"):
            entries.append((name, rewrite_url(s.get("url") or "", src.get("rewrite")), ""))
        elif isinstance(s, dict) and s.get("source") == "git-subdir":
            entries.append((name, rewrite_url(s.get("url") or "", src.get("rewrite")), (s.get("path") or "").strip("/")))
        else:
            entries.append((name, None, f"不支持的 source 类型：{s!r}"))
    return entries, (idx_dir, rev)


def sync_source(source_id):
    """同步一个技能源。返回报告 dict；同时写回 skill_sources 的 last_* 字段。"""
    src = store.get_skill_source(source_id)
    if not src:
        raise SyncError("技能源不存在")
    with _sync_lock:
        return _sync_locked(src)


def _sync_locked(src):
    report = {"plugins": [], "added": [], "updated": [], "removed": [], "unchanged": [],
              "conflicts": [], "started": time.time()}
    workdir = tempfile.mkdtemp(prefix="skillsync-")
    allowed_host = _host(rewrite_url(src["url"], src.get("rewrite")))
    wanted = set(src.get("plugins") or [])
    try:
        try:
            entries, index = _plugin_entries(src, workdir)
        except SyncError as e:
            store.set_skill_source_result(src["id"], "error", str(e), report)
            report["error"] = str(e)
            return report

        existing = {(s["plugin"], s["path"]): s for s in store.skills_by_source(src["id"])}
        seen, ok_plugins = set(), set()
        for i, (pname, url, sub) in enumerate(entries):
            if src.get("kind") == "marketplace" and wanted and pname not in wanted:
                continue
            item = {"plugin": pname, "url": url, "status": "ok", "skills": []}
            report["plugins"].append(item)
            try:
                if url is None:
                    raise SyncError(sub)
                if url == "@index":
                    root, rev = Path(index[0]) / sub, index[1]
                    if not _inside(index[0], root):
                        raise SyncError(f"索引里的相对路径 {sub} 跳出了索引仓，拒绝")
                else:
                    host = _host(url)
                    if host and allowed_host and host != allowed_host:
                        raise SyncError(f"地址主机 {host} 与技能源主机 {allowed_host} 不一致，拒绝（不把令牌发到别处）")
                    dest = Path(workdir) / f"p{i}"
                    token = src.get("token") if host == allowed_host else ""
                    rev = _clone(url, None if src.get("kind") == "marketplace" else src.get("branch"),
                                 dest, src.get("username"), token)
                    root = dest / sub if sub else dest
                    if not _inside(dest, root):
                        raise SyncError(f"子路径 {sub} 跳出了仓库目录，拒绝")
                if not root.is_dir() or root.is_symlink():
                    raise SyncError(f"仓内找不到路径 {sub}")
                pname = pname or _plugin_name(root, Path(urlparse(url).path).stem.replace("skill-", ""))
                item.update(plugin=pname, revision=rev[:12])
                ok_plugins.add(pname)
                for sk in discover(root):
                    key = (pname, sk["path"])
                    seen.add(key)
                    item["skills"].append(sk["name"])
                    if sk["skipped"]:
                        item.setdefault("skipped_files", []).extend(f"{sk['name']}/{f}" for f in sk["skipped"])
                    old = existing.get(key)
                    clash = store.get_skill_by_name(sk["name"])
                    if clash and (not old or clash["id"] != old["id"]):
                        report["conflicts"].append(f"{pname}/{sk['path']} → 名称「{sk['name']}」已被"
                                                   + ("手工技能" if not clash.get("source_id") else "其他来源") + "占用，跳过")
                        continue
                    store.upsert_git_skill(src["id"], pname, sk["path"], sk["name"], sk["description"],
                                           sk["content"], sk["files"], rev, skill_id=old["id"] if old else None)
                    bucket = "updated" if old and old.get("revision") != rev else ("unchanged" if old else "added")
                    report[bucket].append(sk["name"])
            except SyncError as e:
                item.update(status="error", error=str(e))
            except Exception as e:
                item.update(status="error", error=f"{type(e).__name__}: {e}")

        # 删除：只删「所属能力仓这次拉取成功」或「已不在索引/名单里」的旧技能；拉取失败的保留上一版
        listed = {e[0] for e in entries}
        for key, s in existing.items():
            if key in seen:
                continue
            plugin = key[0]
            gone_from_index = src.get("kind") == "marketplace" and (plugin not in listed or (wanted and plugin not in wanted))
            if plugin in ok_plugins or gone_from_index:
                store.delete_skill(s["id"])
                report["removed"].append(s["name"])

        errors = [f"{p['plugin']}: {p['error']}" for p in report["plugins"] if p["status"] == "error"]
        if wanted:
            missing = wanted - {p["plugin"] for p in report["plugins"]}
            errors += [f"{m}: 索引里没有这个 plugin" for m in sorted(missing)]
        status = "ok" if not errors and not report["conflicts"] else ("partial" if ok_plugins else "error")
        report["finished"] = time.time()
        store.set_skill_source_result(src["id"], status, "；".join(errors + report["conflicts"]), report)
        report["status"] = status
        return report
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def sync_due(now=None):
    """调度线程调：同步到期的启用技能源。已有同步在跑就跳过本轮。"""
    now = now or time.time()
    if not _sync_lock.acquire(blocking=False):
        return []
    try:
        done = []
        for src in store.list_skill_sources():
            if not src["enabled"]:
                continue
            interval = max(5, int(src.get("interval_min") or 60)) * 60
            if src.get("last_sync") and now - src["last_sync"] < interval:
                continue
            try:
                rep = _sync_locked(src)
                done.append((src["id"], rep.get("status") or "error"))
                print(f"[技能同步] {src['name']} → {rep.get('status') or rep.get('error')}"
                      f"（新增 {len(rep['added'])}、更新 {len(rep['updated'])}、删除 {len(rep['removed'])}）", flush=True)
            except Exception as e:
                store.set_skill_source_result(src["id"], "error", f"{type(e).__name__}: {e}")
        return done
    finally:
        _sync_lock.release()
