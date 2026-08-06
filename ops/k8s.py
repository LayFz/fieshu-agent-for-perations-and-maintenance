"""K8s 集群运维执行：给数字员工用 kubectl 直连集群 API server 跑命令，实现「看异常 pod / 查主备 / 生效密钥重启」这类集群运维。

⚠️ 这是继 ssh_exec 之后全平台第二高风险的能力（生产 k8s 集群 + 群里触发的大模型）。三层闸门（同 ops/ssh.py）：
  1) 模型判断为主——工具说明里写死了「先判断是否危险/可逆，危险就拒绝并解释」，并叠加 GitOps 护栏
     （业务资源变更走 gitops git push，别 kubectl 改，否则被 ArgoCD selfHeal 覆盖/当孤儿删）；
     再叠加应用系统提示词 + k8s-ops 技能（渐进披露）。
  2) 硬兜底黑名单——只挡「不可逆/毁灭级」命令（delete cluster/pvc/pv/ns/node、delete --all），
     挡的是「模型判断失灵」的最坏情况，不替代模型判断，也不挡正常运维（get/describe/logs/rollout restart/failover）。
     另叠加集群级 read_only 开关：标了只读的集群，任何 mutating 命令直接拒。
  3) 隔离——应用在后台勾了 kubectl 工具才有；绝不给「主动旁听」类应用勾这个。

集群与 kubeconfig 存 kv：k8s_clusters(JSON 列表) / k8s_blacklist(JSON 正则列表，留空用默认)。
kubectl 用打包进镜像的二进制，subprocess 调用、不经 shell（保证「只能 kubectl」且防注入）；kubeconfig 每次写临时文件、用完删。
"""
import json
import os
import re
import shlex
import subprocess
import tempfile

from core import store

# 硬兜底黑名单：只列 k8s「不可逆/毁灭级」正则。正常运维（看日志、rollout restart、CNPG failover）不在此列——那交给模型判断。
_DEFAULT_BLACKLIST = [
    r"\bdelete\b.*\bcluster",                                   # delete cluster/clusters：CNPG 连带删 PVC/数据
    r"\bdelete\b.*\b(pvc|persistentvolumeclaims?|pv|persistentvolumes?)\b",  # 删存储卷=数据丢失
    r"\bdelete\b.*\b(ns|namespaces?)\b",                        # 删命名空间=连带删里面所有资源
    r"\bdelete\b.*\bnodes?\b",                                  # 删节点对象
    r"\bdelete\b.*--all\b",                                     # delete xxx --all：批量清空
]

# 只读 verb 白名单：集群标了 read_only 时，只放行这些。
_READONLY_VERBS = {
    "get", "describe", "logs", "top", "explain", "events", "wait",
    "api-resources", "api-versions", "version", "cluster-info", "config", "diff",
}
# 会改动集群的 verb（用于从命令里认出「真正的动词」，不被 -n/-l 的取值骗到）。
_WRITE_VERBS = {
    "create", "apply", "edit", "patch", "replace", "delete", "scale", "rollout",
    "autoscale", "expose", "run", "set", "label", "annotate", "taint",
    "cordon", "uncordon", "drain", "exec", "cp", "attach", "port-forward",
    "proxy", "debug", "kustomize",
}
_ALL_VERBS = _READONLY_VERBS | _WRITE_VERBS


def clusters():
    try:
        return json.loads(store.get_setting("k8s_clusters", "[]") or "[]")
    except Exception:
        return []


def blacklist():
    raw = store.get_setting("k8s_blacklist")
    if raw:
        try:
            v = json.loads(raw)
            if isinstance(v, list):
                return v
        except Exception:
            pass
    return _DEFAULT_BLACKLIST


def _aliases(c):
    """一个集群的全部「叫法」：名字 + 别名列表。别名支持字符串(逗号/空格/换行分隔)或数组。"""
    raw = c.get("aliases")
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    names = [c.get("name")] + list(raw or [])
    return [str(x).strip() for x in names if x and str(x).strip()]


def _find_cluster(target):
    """按 target 解析受管集群：名字 / 任意别名都能命中。只有一个集群时可省略 target；多个必须指名，避免打错集群。"""
    cs = clusters()
    if not cs:
        return None, cs
    if not target:
        return (cs[0] if len(cs) == 1 else None), cs
    t = str(target).strip().lower()
    for c in cs:
        if any(a.lower() == t for a in _aliases(c)):
            return c, cs
    return None, cs


def _blocked(command):
    for pat in blacklist():
        try:
            if re.search(pat, command):
                return pat
        except re.error:
            continue
    return None


def _first_verb(argv):
    """认出真正的 kubectl 动词：跳过 -n/-l 这类旗标【及其取值】，返回第一个已知动词。
    例 '-n argocd get applications' -> 'get'（而不是把 -n 的取值 argocd 当动词）。
    找不到已知动词时退回第一个非旗标 token（未知命令，交给只读白名单去拦，安全）。"""
    for tok in argv:
        if tok.startswith("-"):
            continue
        if tok in _ALL_VERBS:
            return tok
        # 非旗标、又不是已知动词：多半是某旗标的取值（如 -n 后的 argocd），跳过继续找
    for tok in argv:
        if not tok.startswith("-"):
            return tok
    return ""


def list_clusters():
    """列出受管集群（不含 kubeconfig）。read_only=只读集群，任何变更命令都会被拒。"""
    out = []
    for c in clusters():
        extra = [a for a in _aliases(c) if a != c.get("name")]
        out.append({"name": c.get("name"), "aliases": extra,
                    "read_only": bool(c.get("read_only"))})
    return {"clusters": out}


def run(target, command, timeout=30):
    """在受管集群 target 上跑一条 kubectl 命令（command 是 kubectl 子命令，不含前导 kubectl，如 'get pods -A'）。
    返回 exit_code/stdout/stderr。"""
    command = (command or "").strip()
    if not command:
        return {"error": "命令为空"}
    # 容错：模型可能把前导 kubectl 也带上，去掉
    command = re.sub(r"^\s*kubectl\s+", "", command)

    c, cs = _find_cluster(target)
    if not c:
        avail = [{"name": x.get("name"),
                  "aliases": [a for a in _aliases(x) if a != x.get("name")],
                  "read_only": bool(x.get("read_only"))} for x in cs]
        return {"error": f"目标集群「{target or '(未指定)'}」不在受管列表里，无法操作（安全隔离：只有后台"
                         f"「K8s 运维」页配过的集群才能连）。当前可用：{avail or '（一个都还没配）'}。"
                         f"若这个集群确实该管，请去后台添加它、或把「{target}」加进对应集群的别名后再试。",
                "hint_for_user": "去后台「K8s 运维」页添加该集群或补充其别名"}

    try:
        argv = shlex.split(command)
    except ValueError as e:
        return {"error": f"命令解析失败（引号不配对？）：{e}"}
    if not argv:
        return {"error": "命令为空"}

    # 只读集群闸门：非只读 verb 直接拒
    if c.get("read_only") and _first_verb(argv) not in _READONLY_VERBS:
        return {"error": f"集群「{c.get('name')}」被设为只读（后台「K8s 运维」页的 read_only 开关），"
                         f"只允许只读命令（{'、'.join(sorted(_READONLY_VERBS))}）。"
                         f"当前命令动词「{_first_verb(argv)}」不在其中，已拒绝。若确需变更，请人工操作或去后台关掉只读。"}

    bad = _blocked(command)
    if bad:
        return {"error": f"命令被安全黑名单拦截（这是不可逆/毁灭级操作，已拒绝执行）。"
                         f"若确属必要，请人工在集群上操作，别让机器人来。pattern={bad}"}

    kubeconfig = c.get("kubeconfig") or ""
    if not kubeconfig.strip():
        return {"error": f"集群「{c.get('name')}」还没配 kubeconfig，无法连接。请去后台「K8s 运维」页粘贴 kubeconfig。"}

    try:
        timeout = max(1, min(int(timeout or 30), 300))
    except Exception:
        timeout = 30

    # kubeconfig 写临时文件（0600），用完删；kubectl 直连 API，不经 shell
    fd, path = tempfile.mkstemp(prefix="kubeconfig-", suffix=".yaml")
    try:
        os.write(fd, kubeconfig.encode("utf-8"))
        os.close(fd)
        os.chmod(path, 0o600)
    except Exception as e:
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            os.unlink(path)
        except Exception:
            pass
        return {"error": f"写 kubeconfig 临时文件失败：{e}"}

    try:
        proc = subprocess.run(
            ["kubectl", "--kubeconfig", path, *argv],
            capture_output=True, text=True, timeout=timeout,
        )
        out = proc.stdout or ""
        err = proc.stderr or ""
        return {"cluster": c.get("name"), "exit_code": proc.returncode,
                "stdout": out[:8000], "stderr": err[:2000],
                "truncated": len(out) > 8000}
    except FileNotFoundError:
        return {"error": "找不到 kubectl 可执行文件（容器镜像未装 / 本地未安装）。"
                         "容器请确认 Dockerfile 已装 kubectl；本地开发需 `brew install kubectl`。"}
    except subprocess.TimeoutExpired:
        return {"error": f"kubectl 执行超时（>{timeout}s）。可能是网络到不了集群 API server，"
                         f"或命令本身耗时（如 wait/-f 跟随）。"}
    except Exception as e:
        return {"error": f"在集群 {c.get('name')} 执行失败：{e}"}
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
