#!/usr/bin/env python3
"""在 Harbor 主机上运行一次：开启漏洞扫描并为 harness 建只读 robot。

    sudo python3 harbor_enable_scanning.py

做的事（均通过 Harbor API，管理员账号只在本次运行中使用，不写盘、不回显）：
  1. 立即触发一次全量扫描；
  2. 设置每天 03:00 定时全量扫描；
  3. 所有项目开启「推送即扫描」(auto_scan)；
  4. 创建系统级只读 robot「harness-readonly」（所有项目：仓库列表/拉取、制品列表/读取、制品附加信息读取、扫描读取），
     打印一次其账号名与密钥——请复制到 harness 后台「连接凭证 → Harbor 镜像扫描」。
已存在同名 robot 时不重建、不改密钥（需要新密钥请在 Harbor 网页上刷新）。
"""
import base64
import getpass
import json
import re
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1/api/v2.0"
HARBOR_YML = "/opt/harbor/harbor.yml"
ROBOT = "harness-readonly"
DAILY_CRON = "0 0 3 * * *"


def call(method, path, auth, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "Authorization": "Basic " + auth, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode("utf-8", "replace")[:300]


def admin_auth():
    pw = ""
    try:
        m = re.search(r"^harbor_admin_password:\s*(\S+)", open(HARBOR_YML).read(), re.M)
        pw = m.group(1) if m else ""
    except OSError:
        pass
    for attempt in range(2):
        if pw:
            auth = base64.b64encode(f"admin:{pw}".encode()).decode()
            status, _ = call("GET", "/users/current", auth)
            if status == 200:
                return auth
            print("harbor.yml 里的管理员密码不可用（可能已在网页上改过）。" if attempt == 0 else "密码不对。")
        pw = getpass.getpass("请输入 Harbor admin 密码（不回显）：")
    sys.exit("无法以管理员身份登录 Harbor，已停止，未做任何修改。")


def main():
    auth = admin_auth()
    print("✓ 已以 admin 登录")

    status, body = call("POST", "/system/scanAll/schedule", auth, {"schedule": {"type": "Manual"}})
    print("✓ 已触发全量扫描" if status in (200, 201) else f"! 触发全量扫描返回 {status}：{body}")

    sched = {"schedule": {"type": "Custom", "cron": DAILY_CRON}}
    status, body = call("PUT", "/system/scanAll/schedule", auth, sched)
    if status not in (200, 201):
        status, body = call("POST", "/system/scanAll/schedule", auth, sched)
    print("✓ 已设置每天 03:00 全量扫描" if status in (200, 201) else f"! 设置定时扫描返回 {status}：{body}")

    page, done, failed = 1, [], []
    while True:
        status, projects = call("GET", f"/projects?page={page}&page_size=100", auth)
        if status != 200 or not projects:
            break
        for p in projects:
            if (p.get("metadata") or {}).get("auto_scan") == "true":
                done.append(p["name"])
                continue
            s, b = call("PUT", f"/projects/{p['project_id']}", auth, {"metadata": {"auto_scan": "true"}})
            (done if s == 200 else failed).append(p["name"] if s == 200 else f"{p['name']}({s})")
        if len(projects) < 100:
            break
        page += 1
    print(f"✓ 推送即扫描已开启：{len(done)} 个项目" + (f"；失败：{failed}" if failed else ""))

    status, robots = call("GET", f"/robots?q=name%3D~{ROBOT}&page_size=50", auth)
    if status == 200 and any(r.get("name", "").endswith(ROBOT) for r in robots or []):
        name = next(r["name"] for r in robots if r.get("name", "").endswith(ROBOT))
        print(f"✓ robot 已存在：{name}（未改动；如需新密钥请在 Harbor「机器人账户」里刷新）")
        return
    access = [{"resource": r, "action": a} for r, a in (
        ("repository", "list"), ("repository", "pull"), ("artifact", "list"), ("artifact", "read"),
        ("artifact-addition", "read"), ("scan", "read"))]
    status, body = call("POST", "/robots", auth, {
        "name": ROBOT, "description": "feishu-agent harness 只读：读取镜像扫描结果", "level": "system",
        "duration": -1, "disable": False,
        "permissions": [{"kind": "project", "namespace": "*", "access": access}]})
    if status != 201:
        sys.exit(f"! 创建 robot 失败 {status}：{body}")
    print("\n================ 只显示这一次 ================")
    print(f"robot 账号：{body['name']}")
    print(f"密钥：      {body['secret']}")
    print("填到 harness 后台「连接凭证 → Harbor 镜像扫描」后点「测试连接」。")
    print("============================================")


if __name__ == "__main__":
    main()
