"""公共镜像仓库的 tag 查询（只读）：给第三方镜像找「同一版本线里更新的版本」。

目前支持 Docker Hub（含经自家 Harbor 代理缓存的 dockerhub 项目）。其他仓库（quay.io、ghcr.io、registry.k8s.io）
暂不查询，修复方案里标注「需人工确认目标版本」。走后台配置的出网代理（web_proxy + 抓取开关），不读环境代理。
"""
import re

import requests

from core import components
from . import web

_TIMEOUT = 15
_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)(.*)$")


def hub_repo(image, own_registries=()):
    """镜像引用 → Docker Hub 仓库名（library/redis）；不是 Docker Hub 镜像返回 None。"""
    registry, path, _tag, _ = components.parse_image(image)
    if registry and any(registry == r or registry.endswith("." + r) for r in own_registries):
        project, _, rest = path.partition("/")
        if project.lower() not in ("dockerhub", "docker.io") or not rest:
            return None
        registry, path = "", rest
    if registry not in ("", "docker.io", "index.docker.io", "registry-1.docker.io", "docker.m.daocloud.io"):
        return None
    return path if "/" in path else f"library/{path}"


def tags(repo, name_filter="", pages=3):
    """列出 Docker Hub 仓库的 tag（按最近更新排序，最多 pages×100 个）。失败返回 None。"""
    session = requests.Session()
    session.trust_env = False
    proxy = web.proxy_for("fetch")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    out = []
    url = f"https://hub.docker.com/v2/repositories/{repo}/tags"
    params = {"page_size": 100, "ordering": "last_updated", "name": name_filter}
    try:
        for _ in range(pages):
            r = session.get(url, params=params, timeout=_TIMEOUT)
            if r.status_code != 200:
                return None if not out else out
            data = r.json()
            out += [t.get("name") for t in data.get("results") or [] if t.get("name")]
            url, params = data.get("next"), None
            if not url:
                break
    except (requests.RequestException, ValueError):
        return None if not out else out
    finally:
        session.close()
    return out


def split(tag):
    """'7.4.11-alpine' → ((7, 4, 11), '-alpine')；无法解析返回 (None, tag)。"""
    m = _VERSION.match(tag or "")
    if not m:
        return None, tag or ""
    return tuple(int(x) for x in m.group(1).split(".")), m.group(2)


def suggest(current, available):
    """在同一变体（后缀相同，如 -alpine / -management-alpine）、同一大版本里，找比当前更新的最高版本；
    优先完整版本号（便于固定、强制拉到新构建）。返回 (建议 tag, 说明)。"""
    cur, suffix = split(current)
    if cur is None:
        return None, "当前 tag 不是版本号（如 latest），无法自动推荐"
    best = None
    for t in available or []:
        ver, suf = split(t)
        if ver is None or suf != suffix or ver[0] != cur[0]:
            continue
        if len(cur) >= 2 and len(ver) >= 2 and ver[:2] < cur[:2]:
            continue
        key = (ver, len(ver))
        if ver >= cur and (best is None or key > best[0]):
            best = (key, t)
    if not best:
        return None, f"没有找到同一大版本（{cur[0]}.x{suffix}）里更新的 tag"
    target = best[1]
    tv, _ = split(target)
    if tv == cur:
        return target, "已是同一版本线的最新版本号"
    if len(tv) > len(cur) and tv[:len(cur)] == cur:
        return target, (f"当前是浮动 tag（{current}），节点上可能还是旧构建；固定到 {target} 会强制拉取最新构建，"
                        "版本线不变")
    kind = "补丁版本" if tv[:2] == cur[:2] else "小版本"
    return target, f"同一大版本内的{kind}升级"
