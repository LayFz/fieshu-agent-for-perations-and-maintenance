"""应用内的只读 GitOps 采集；不执行仓库代码，不把 Secret 载荷写入证据。

gitops_repositories 是管理员配置的 project/url/branch 列表。SSH 使用运行账户的
已有密钥和 known_hosts；主机身份必须已确认。无 git 二进制时用 Paramiko/Dulwich。
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

from dulwich.client import SSHGitClient, SSHVendor
from dulwich.repo import Repo
from dulwich.objects import Blob, Tree
import paramiko
import yaml

from core import store
from ops import gitlab


def configured():
    try:
        rows = json.loads(store.get_setting("gitops_repositories", "[]") or "[]")
        return rows if isinstance(rows, list) else []
    except ValueError:
        return []


def _project(url):
    return urlsplit(url or "").path.strip("/").removesuffix(".git")


def validate_repository(config):
    if not isinstance(config, dict) or set(config) != {"project", "url", "branch"}:
        raise ValueError("仓库连接必须包含 project、url、branch")
    project, address, branch = config["project"], config["url"], config["branch"]
    if (not isinstance(project, str) or not project or len(project) > 255 or
            any(not part or part in (".", "..") or not all(c.isalnum() or c in "_.-" for c in part)
                for part in project.split("/"))):
        raise ValueError("GitOps 项目路径无效")
    if not isinstance(address, str) or len(address) > 2048:
        raise ValueError("仓库 SSH 地址无效")
    try:
        url = urlsplit(address)
        port = url.port
    except ValueError:
        raise ValueError("仓库 SSH 地址无效") from None
    if (url.scheme != "ssh" or url.username != "git" or url.password or
            not url.hostname or url.query or url.fragment or url.path != "/" + project + ".git" or
            (port is not None and not 1 <= port <= 65535)):
        raise ValueError("仓库必须配置无内嵌口令的 git 用户 SSH 地址，路径须匹配 project")
    if (not isinstance(branch, str) or not branch or len(branch) > 200 or branch.startswith("-") or
            any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/" for c in branch)
            or ".." in branch or branch.endswith(("/", "."))):
        raise ValueError("无效分支")
    return {"project": project, "url": address, "branch": branch}


class _SSHChannel:
    def __init__(self, client, channel):
        self.client, self.channel = client, channel
        self.stderr = channel.makefile_stderr("rb")

    def read(self, length):
        if length is None or length < 0:
            chunks = []
            while part := self.channel.recv(65536):
                chunks.append(part)
            return b"".join(chunks)
        out = bytearray()
        while len(out) < length:
            part = self.channel.recv(length - len(out))
            if not part:
                break
            out.extend(part)
        return bytes(out)

    def write(self, data):
        self.channel.sendall(data)
        return len(data)

    def can_read(self):
        return self.channel.recv_ready()

    def close(self, timeout=60):
        self.channel.close()
        self.client.close()


class _ParamikoVendor(SSHVendor):
    def run_command(self, host, command, username=None, port=None, password=None,
                    key_filename=None, ssh_command=None, protocol_version=None):
        if password or ssh_command not in (None, "ssh"):
            raise ValueError("不支持通过仓库配置传入 SSH 口令或自定义命令")
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            if key_filename is None:
                ssh_config = Path.home() / ".ssh" / "config"
                if ssh_config.is_file():
                    with ssh_config.open(encoding="utf-8") as stream:
                        entry = paramiko.SSHConfig.from_file(stream).lookup(host)
                    candidates = [str(Path(name).expanduser()) for name in entry.get("identityfile", [])]
                    key_filename = candidates or None
            client.connect(host, port=port or 22, username=username or "git", key_filename=key_filename,
                           look_for_keys=True, allow_agent=True, timeout=10, auth_timeout=10, banner_timeout=10)
            transport = client.get_transport()
            if transport is None:
                raise RuntimeError("SSH 传输不可用")
            channel = transport.open_session(timeout=10)
            channel.settimeout(90)
            channel.exec_command(command.decode("utf-8"))
            return _SSHChannel(client, channel)
        except Exception:
            client.close()
            raise


def _clone_paramiko(config, path):
    address = urlsplit(config["url"])
    client = SSHGitClient(address.hostname, port=address.port, username="git", vendor=_ParamikoVendor())
    repo = client.clone(address.path, str(path), mkdir=False, bare=True, checkout=False,
                        branch=config["branch"], depth=1, progress=lambda _: None,
                        protocol_version=0, origin_url=config["url"])
    repo.close()


def _clone(config, path):
    config = validate_repository(config)
    if shutil.which("git") is None:
        try:
            _clone_paramiko(config, path)
            return
        except Exception:
            raise RuntimeError("Git SSH 读取失败；检查运行账户的密钥、known_hosts 和仓库权限") from None
    branch = config["branch"]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull,
               GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10",
               GIT_ALLOW_PROTOCOL="ssh")
    proc = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "clone", "--bare", "--depth=1",
                           "--single-branch", "--branch", branch, "--", config["url"], str(path)],
                          env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE, timeout=90)
    if proc.returncode:
        raise RuntimeError("Git SSH 读取失败；检查运行账户的密钥、known_hosts 和仓库权限")


def _files(repo):
    files, budget = {}, [0]
    def walk(tree_id, prefix=""):
        tree = repo[tree_id]
        if not isinstance(tree, Tree):
            return
        for entry in tree.iteritems():
            budget[0] += 1
            if budget[0] > 10000:
                raise ValueError("仓库文件数量超过巡检上限")
            name = entry.path.decode("utf-8", "replace")
            rel = prefix + name
            if entry.mode == 0o40000:
                walk(entry.sha, rel + "/")
            elif entry.mode in (0o100644, 0o100755) and rel.endswith((".yaml", ".yml", ".json")):
                obj = repo[entry.sha]
                if isinstance(obj, Blob):
                    if obj.raw_length() > 2 * 1024 * 1024:
                        raise ValueError("配置文件超过巡检大小上限")
                    files[rel] = obj.data
    walk(repo[repo.head()].tree)
    if sum(map(len, files.values())) > 25 * 1024 * 1024:
        raise ValueError("配置总量超过巡检上限")
    return files


def _normalize(base, rel):
    if not isinstance(rel, str) or ":" in rel or rel.startswith("/"):
        raise ValueError("暂不解析远程 Kustomize 资源")
    parts = []
    for p in PurePosixPath(base, rel).parts:
        if p == "..":
            if not parts:
                raise ValueError("资源路径越界")
            parts.pop()
        elif p != ".":
            parts.append(p)
    return "/".join(parts)


def inspect(files, paths, project, revision):
    """只解析纳管 Kustomization resources；返回事实型风险及完整性。"""
    selected, seen, gaps = set(), set(), []
    def visit(path):
        if path in seen:
            return
        seen.add(path)
        if len(seen) > 3000:
            raise ValueError("Kustomize 资源数量超过上限")
        if path in files:
            selected.add(path)
            return
        kfile = next((path + "/" + f for f in ("kustomization.yaml", "kustomization.yml")
                      if path + "/" + f in files), None)
        if not kfile:
            gaps.append("未找到目录中的 Kustomization")
            return
        k = yaml.safe_load(files[kfile]) or {}
        if not isinstance(k, dict):
            raise ValueError("Kustomization 格式错误")
        if any(k.get(f) for f in ("helmCharts", "generators", "secretGenerator", "patches", "patchesStrategicMerge", "patchesJson6902", "components")):
            gaps.append("未完整渲染 charts/generators/patches/components")
        for item in list(k.get("resources") or []) + list(k.get("bases") or []):
            try:
                visit(_normalize(path, item))
            except ValueError:
                gaps.append("资源路径超出离线解析范围")
    for path in paths:
        visit(_normalize("", path))
    findings, counts = [], {"manifest_files": len(selected), "native_secrets_with_data": 0, "workloads": 0}
    source = "gitops:" + project
    for path in sorted(selected):
        for index, obj in enumerate(yaml.safe_load_all(files[path])):
            if not isinstance(obj, dict):
                continue
            kind = obj.get("kind")
            md, spec = obj.get("metadata") or {}, obj.get("spec") or {}
            name = str(md.get("name") or "unnamed")[:150]
            evidence = {"project": project, "revision": revision, "file": path,
                        "document": index + 1, "kind": kind, "name": name}
            def add(code, severity, title, detail):
                findings.append({"key": f"{source}:{path}:{index}:{code}", "source": source,
                    "severity": severity, "title": title, "detail": detail,
                    "resource": f"{project}/{path}#{name}", "evidence": evidence})
            if kind == "Secret" and any(any(bool(v) for v in (obj.get(field) or {}).values()) for field in ("data", "stringData")):
                counts["native_secrets_with_data"] += 1
                add("native-secret", "high", "GitOps 跟踪了原生 Secret 数据",
                    "确认存在非空 data/stringData；未读取凭据有效性，内容不会进入报告。请核验并安排密钥轮换/加密管理。")
            if kind == "Cluster" and obj.get("apiVersion", "").startswith("postgresql.cnpg.io/"):
                if not spec.get("backup") and not spec.get("plugins"):
                    add("backup-config", "high", "CNPG 配置未声明托管备份",
                        "声明中没有 backup/plugins；外部备份未检查，需结合实时备份对象核对覆盖。")
            if kind in ("Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job"):
                counts["workloads"] += 1
    return findings, counts, sorted(set(gaps))


def scan(project, applications):
    source = "gitops:" + project
    result = {"observed_at": datetime.now(timezone.utc).isoformat(), "sources": [], "findings": [], "summary": {}}
    config = next((r for r in configured() if r.get("project") == project), None)
    mapped = [(s["path"], app.get("compared_revision"), app.get("sync"))
              for app in applications for s in app.get("sources", [])
              if s.get("path") and _project(s.get("repo_url") or s.get("repo")) == project and not s.get("chart")]
    paths = sorted({path for path, _, _ in mapped})
    try:
        if not config:
            raise ValueError("未配置该 GitOps 仓库连接")
        if not paths:
            raise ValueError("本轮未获取该仓库的 ArgoCD 纳管路径，未把整个仓库冒充部署配置")
        with tempfile.TemporaryDirectory(prefix="feishu-gitops-") as temp:
            _clone(config, temp)
            with Repo(temp) as repo:
                revision = repo.head().decode()
                matching = sorted({path for path in paths if all(compared == revision and sync == "Synced"
                                    for candidate, compared, sync in mapped if candidate == path)})
                gaps = []
                if len(matching) != len(paths):
                    gaps.append("部分应用比较版本与仓库 HEAD 不同或尚未同步；其路径未按当前部署配置扫描")
                findings, counts, parse_gaps = inspect(_files(repo), matching, project, revision)
                gaps.extend(parse_gaps)
        result["findings"] = findings
        result["summary"] = {"project": project, "revision": revision, "application_paths": matching,
                             "unverified_paths": sorted(set(paths) - set(matching)), **counts}
        result["sources"].append({"name": source, "status": "partial" if gaps else "ok",
                                  "error": "; ".join(gaps), "revision": revision})
    except Exception as exc:
        # 仓库配置和异常可能携带数据，仅输出已定义的安全原因或异常类型。
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        result["sources"].append({"name": source, "status": "error", "error": message[:200]})
    pipe_source = "gitlab:" + project + ":pipelines"
    if gitlab.has_token(project):
        details = gitlab.project_details(project)
        pipelines = gitlab.pipelines(project, limit=5)
        error = details.get("error") or pipelines.get("error")
        result["sources"].append({"name": pipe_source, "status": "error" if error else "ok", "error": error or ""})
        if not error:
            result["summary"]["default_branch_commit"] = details.get("default_branch_commit")
            result["summary"]["pipelines"] = pipelines.get("pipelines")
            for p in (pipelines.get("pipelines") or [])[:1]:
                if p.get("status") == "failed":
                    result["findings"].append({"key": pipe_source + ":latest-failed", "source": pipe_source,
                        "severity": "medium", "title": "GitLab 最近流水线失败", "detail": "需核对流水线任务日志；未读取变量或制品。",
                        "resource": project, "evidence": p})
    else:
        result["sources"].append({"name": pipe_source, "status": "error", "error": "未配置该项目的 GitLab API 凭据；Git SSH 只提供仓库扫描"})
    return result
