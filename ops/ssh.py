"""SSH 运维执行：给数字员工在受管服务器上跑命令，实现「网掉了让它 sudo wg-quick up wg0」这类运维。

⚠️ 这是全平台风险最高的能力（root 生产机 + 群里触发的大模型）。三层闸门：
  1) 模型判断为主——工具说明里写死了「先判断是否危险/可逆，危险就拒绝并解释」，
     再叠加应用自己的系统提示词 + 网络拓扑技能（渐进披露）。
  2) 硬黑名单兜底——只挡「不可逆/毁灭级」命令（rm -rf /、mkfs、dd 到磁盘等），
     挡的是「模型判断失灵」的最坏情况，不替代模型判断，也不挡正常运维（systemctl/wg-quick/重启等）。
  3) 隔离——应用在后台勾了 ssh_exec 工具才有；绝不给「主动旁听」类应用勾这个。

主机与凭证存 kv：ssh_hosts(JSON 列表) / ssh_blacklist(JSON 正则列表，留空用默认)。
"""
import io
import json
import re

import paramiko

from core import store

# 硬兜底黑名单：只列「不可逆/毁灭级」正则。正常运维命令（重启服务、起网卡、看日志）不在此列——那交给模型判断。
_DEFAULT_BLACKLIST = [
    r"\brm\s+-[a-zA-Z]*\s+(/|/\s|/\*|~|\$HOME)(\s|$)",   # rm -rf / 、rm -rf ~ 之类
    r"\bmkfs\b", r"\bwipefs\b", r"\bfdisk\b", r"\bparted\b",
    r"\bdd\b[^\n|]*\bof=/dev/",                            # dd of=/dev/sdX
    r">\s*/dev/(sd|nvme|vd|xvd)",                          # 覆盖裸盘
    r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:",                    # fork 炸弹 :(){ :|:& };:
    r"\bchmod\s+-R\s+[0-7]*\s+/(\s|$)",                    # chmod -R 777 /
    r"\bchown\s+-R\s+\S+\s+/(\s|$)",
    r"\bmv\s+\S+\s+/dev/null\b",
]


def hosts():
    try:
        return json.loads(store.get_setting("ssh_hosts", "[]") or "[]")
    except Exception:
        return []


def blacklist():
    raw = store.get_setting("ssh_blacklist")
    if raw:
        try:
            v = json.loads(raw)
            if isinstance(v, list):
                return v
        except Exception:
            pass
    return _DEFAULT_BLACKLIST


def _aliases(h):
    """一台机的全部「叫法」：名字 + 连接地址 + 别名列表。别名支持字符串(逗号/空格/换行分隔)或数组。"""
    raw = h.get("aliases")
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    names = [h.get("name"), h.get("host")] + list(raw or [])
    return [str(x).strip() for x in names if x and str(x).strip()]


def _find_host(target):
    """按 target 解析受管主机：名字 / 连接地址 / 任意别名都能命中同一台。
    命中后实际连接走的是 h['host']（登记的内网连接地址），与用户嘴上说哪个地址无关。"""
    hs = hosts()
    if not hs:
        return None, hs
    if not target:                   # 只有一台就默认它；多台必须指名，避免打错机器
        return (hs[0] if len(hs) == 1 else None), hs
    t = str(target).strip().lower()
    for h in hs:
        if any(a.lower() == t for a in _aliases(h)):
            return h, hs
    return None, hs


def _blocked(command):
    for pat in blacklist():
        try:
            if re.search(pat, command):
                return pat
        except re.error:
            continue
    return None


def list_hosts():
    """列出受管主机（不含密码）。connect=实际连接地址；aliases=用户可用来指代它的别名/其它地址。"""
    out = []
    for h in hosts():
        extra = [a for a in _aliases(h) if a not in (h.get("name"), h.get("host"))]
        out.append({"name": h.get("name"), "connect": h.get("host"),
                    "aliases": extra, "user": h.get("username", "root"),
                    "port": int(h.get("port", 22))})
    return {"hosts": out}


def run(target, command, timeout=30):
    """在受管主机 target 上跑 command。返回 exit_code/stdout/stderr。"""
    command = (command or "").strip()
    if not command:
        return {"error": "命令为空"}
    h, hs = _find_host(target)
    if not h:
        avail = [{"name": x.get("name"), "connect": x.get("host"),
                  "aliases": [a for a in _aliases(x) if a not in (x.get("name"), x.get("host"))]}
                 for x in hs]
        return {"error": f"目标「{target or '(未指定)'}」不在受管主机列表里，无法操作（安全隔离：只有后台"
                         f"「SSH 运维」页配过的主机才能连）。当前可用：{avail or '（一台都还没配）'}。"
                         f"若这台机确实该管，请去后台添加它、或把「{target}」加进对应主机的别名后再试。",
                "hint_for_user": "去后台「SSH 运维」页添加该主机或补充其别名"}
    bad = _blocked(command)
    if bad:
        return {"error": f"命令被安全黑名单拦截（这是不可逆/毁灭级操作，已拒绝执行）。"
                         f"若确属必要，请人工在服务器上操作，别让机器人来。pattern={bad}"}
    try:
        timeout = max(1, min(int(timeout or 30), 300))
    except Exception:
        timeout = 30

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        pkey = None
        if h.get("key"):
            pkey = paramiko.RSAKey.from_private_key(io.StringIO(h["key"]))
        cli.connect(hostname=h["host"], port=int(h.get("port", 22)),
                    username=h.get("username", "root"),
                    password=h.get("password") or None, pkey=pkey,
                    timeout=10, allow_agent=False, look_for_keys=False)
    except Exception as e:
        cli.close()
        return {"error": f"SSH 连接 {h.get('name')}({h.get('host')}) 失败：{e}"}
    try:
        # 非 root 且配了 sudo 密码：把 sudo 改成读 stdin，喂密码
        sudo_pw = h.get("sudo_password")
        cmd = command
        if sudo_pw and re.match(r"^\s*sudo\b", command):
            cmd = re.sub(r"^\s*sudo\b", "sudo -S -p ''", command, count=1)
        stdin, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
        if sudo_pw and cmd != command:
            try:
                stdin.write(sudo_pw + "\n"); stdin.flush()
            except Exception:
                pass
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        return {"host": h.get("name"), "exit_code": code,
                "stdout": out[:8000], "stderr": err[:2000],
                "truncated": len(out) > 8000}
    except Exception as e:
        return {"error": f"在 {h.get('name')} 执行失败：{e}"}
    finally:
        cli.close()
