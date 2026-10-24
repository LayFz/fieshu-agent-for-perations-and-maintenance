"""修复方案（只读）：针对线上在跑的第三方镜像（redis、mysql、rabbitmq …），找到它在 gitops 仓库里的引用位置，
从上游仓库查同一版本线里更新的 tag，给出「改哪个文件、从什么改成什么、能修掉什么、风险」。

只出方案，不改任何东西。真正修改走已有路径：code_open（gitops 仓库）→ code_edit → code_validate → code_propose_mr（需审批）。
自研镜像（业务仓库）harness 没有源码读权限：只给出要升级的包与基础镜像，交给开发处理。
"""
from urllib.parse import quote, urlsplit

from core import assetstore, components, store
from ops import asset_scan, gitlab, registry
from . import match

# 已停止维护的版本线：同线内升级修不掉大部分漏洞，需要规划大版本升级
EOL = {
    "mysql": {(5, 7): "8.4（LTS）"}, "postgresql": {(11,): "16/17", (12,): "16/17"},
    "python": {(3, 7): "3.12", (3, 8): "3.12"}, "node": {(14,): "22（LTS）", (16,): "22（LTS）", (18,): "22（LTS）"},
    "redis": {(6, 0): "7.4", (5,): "7.4"}, "rabbitmq": {(3, 11): "3.13/4.x", (3, 12): "3.13/4.x"},
}


def _eol(component, version):
    ver = components.parse_version(version)
    for prefix, target in (EOL.get(component) or {}).items():
        if ver and ver[:len(prefix)] == prefix:
            return f"{component} {'.'.join(map(str, prefix))} 已停止维护，同一版本线修不掉大部分漏洞；建议规划升级到 {target}（大版本，需评估兼容性与数据迁移）"
    return None


def gitops_projects(allowed=None):
    """登记为代码仓库的 gitops 项目路径（如 layfz/gitops）；allowed(project)->bool 做权限过滤。"""
    out = []
    for repo in store.list_code_repos():
        if not repo.get("enabled"):
            continue
        path = urlsplit(repo.get("url") or "").path.strip("/").removesuffix(".git")
        if path and (allowed is None or allowed(path)):
            out.append((repo["name"], path))
    return out


def find_refs(projects, image):
    """在 gitops 仓库里搜镜像引用：先搜完整引用，搜不到再搜「名称:tag」。返回 [{repo, project, path, line}]。"""
    registry_host, path, tag, _ = components.parse_image(image)
    short = f"{path.rsplit('/', 1)[-1]}:{tag}" if tag else None
    hits = []
    for term in [t for t in (image.split("@")[0], short) if t]:
        for name, proj in projects:
            data, err = gitlab._get(f"/projects/{quote(proj, safe='')}/search",
                                    params={"scope": "blobs", "search": term}, project=proj)
            if err or not isinstance(data, list):
                continue
            for d in data:
                if term in (d.get("data") or "") and not d.get("path", "").endswith(".md"):
                    hits.append({"repo": name, "project": proj, "path": d.get("path"), "line": d.get("startline"),
                                 "matched": term})
        if hits:
            break
    seen, out = set(), []
    for h in hits:
        key = (h["project"], h["path"])
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out[:10]


def plan(visible=None, allowed_project=None, q=None, limit=10):
    assets = [a for a in assetstore.list_assets(limit=20000) if visible is None or visible(a)]
    findings = match.image_findings(assets, limit=200) if assetstore.list_scans() else {"items": []}
    by_image = {it["image"]: it for it in findings["items"]}
    own = asset_scan.own_registries()
    projects = gitops_projects(allowed_project)
    groups = {}
    for a in assets:
        if a["kind"] != "image" or not a["image"]:
            continue
        if q and q.lower() not in (a["image"] + a["component"]).lower():
            continue
        groups.setdefault(a["image"], []).append(a)

    third_party, self_built = [], []
    for image, mine in groups.items():
        it = by_image.get(image)
        if not it or not it["fixable_high_critical"]:
            continue
        _reg, _path, tag, _ = components.parse_image(image)
        comp = mine[0]["component"]
        repo = registry.hub_repo(image, own)
        if repo:
            target, why = registry.suggest(tag, registry.tags(repo, name_filter=str(components.parse_version(tag)[0])
                                                              if components.parse_version(tag) else ""))
        else:
            target, why = None, "不是 Docker Hub 镜像，需人工确认上游可用的新版本"
        refs = find_refs(projects, image) if projects else []
        third_party.append({
            "image": image, "component": comp, "current_tag": tag, "suggested_tag": target, "why": why,
            "eol": _eol(comp, tag), "priority": it["priority"], "priority_reason": it["priority_reason"],
            "reachability": it["reachability"], "counts": it["counts"], "fixable_high_critical": it["fixable_high_critical"],
            "fix_packages": it["fix_packages"][:3],
            "workloads": sorted({f"{a['cluster']}/{a['namespace']}/{a['workload'].split(' · ')[0]}" for a in mine})[:4],
            "gitops_refs": refs,
            "apply": (f"code_open {refs[0]['repo']} → 把 {refs[0]['path']} 等引用里的 {tag} 改为 {target} → "
                      "code_validate → code_propose_mr（需审批）；合并后 ArgoCD 同步，Harbor 复扫确认") if refs and target else
                     ("gitops 里没找到这个镜像的引用（可能由 Helm values、手工部署或其他仓库管理），需人工定位" if not refs
                      else "没有可自动推荐的目标版本，需人工确认"),
            "verify": "升级后 Harbor 会扫描新镜像；下一次资产刷新后在 vuln_report 中复核该镜像的漏洞是否消失",
        })
    for it in findings["items"]:
        if it["image"] in groups or not it["fixable_high_critical"]:
            continue
        if q and q.lower() not in (it["image"] + it["service"]).lower():
            continue
        if any(a["image"] == it["image"] and a["kind"] == "service" for a in assets):
            self_built.append({"service": it["service"], "image": it["image"], "priority": it["priority"],
                               "reachability": it["reachability"], "fix_packages": it["fix_packages"][:4],
                               "note": "业务仓库 harness 无读权限：交给开发升级上述包/基础镜像后重新构建"})
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    third_party.sort(key=lambda x: (order[x["priority"]], -x["fixable_high_critical"]))
    self_built.sort(key=lambda x: order[x["priority"]])
    return {"third_party": third_party[:limit], "third_party_total": len(third_party),
            "self_built": self_built[:limit], "self_built_total": len(self_built),
            "gitops_repos": [p for _n, p in projects],
            "note": "只出方案，不做修改；修改需经 code_* 工作区、静态校验与审批后提 MR，合并后由 ArgoCD 同步。"}
