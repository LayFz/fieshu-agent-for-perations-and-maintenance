"""修复方案（只读）：针对线上在跑的第三方镜像（redis、mysql、rabbitmq …），找到它在 gitops 仓库里的引用位置，
从上游仓库查同一版本线里更新的 tag，给出「改哪个文件、从什么改成什么、能修掉什么、风险」。

只出方案，不改任何东西。真正修改走已有路径：code_open（gitops 仓库）→ code_edit → code_validate → code_propose_mr（需审批）。
自研镜像只定位问题：按 Harbor 扫描到的包类型推断该改哪类清单（composer.json / go.mod / 基础镜像…），连同线上提交号交给开发；
默认不读业务仓库。后台 kv source_repo_read=1 时才经 GitLab 只读定位到具体文件与行（需令牌与 gitlab_projects 授权）。
"""
import re
from urllib.parse import quote, urlsplit

from core import assetstore, components, store
from ops import asset_scan, gitlab, registry, source_repo
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


_WHERE = {
    "runtime": "Go 版本：Dockerfile 里构建阶段的 FROM golang:x.y，及 go.mod 的 go/toolchain 行",
    "npm": "package.json / 锁文件（package-lock.json、pnpm-lock.yaml、yarn.lock）",
    "go": "go.mod / go.sum（go get 包@修复版本 && go mod tidy）",
    "composer": "composer.json / composer.lock（composer update 包名）",
    "maven": "pom.xml / build.gradle",
    "system": "基础镜像里的系统包：更新 Dockerfile 的 FROM 到最新构建后重新构建",
    "app": "应用依赖清单（requirements.txt / pyproject.toml / package.json 等，按包名确认）",
}


def manifest_hint(package, version=""):
    """由 Trivy 包名/版本推断要改哪类清单：{kind, where}。kind=runtime/system 归入「升级基础镜像」。"""
    kind = components.package_kind(package, version)
    return {"kind": kind, "where": _WHERE[kind]}


def locate_plan(item):
    """不读业务仓库：按包类型分成「应用依赖」与「基础镜像/运行时」，交给开发；修复版本来自 Harbor 扫描。"""
    _reg, _path, tag, _ = components.parse_image(item["image"])
    deps, base = [], []
    for pkg in item["fix_packages"]:
        hint = manifest_hint(pkg["package"], pkg["version"])
        entry = {"package": pkg["package"], "current": pkg["version"], "fix_to": pkg["fixed"],
                 "severity": pkg["severity"], "where": hint["where"], "cves": pkg.get("cves", [])}
        (base if hint["kind"] in ("system", "runtime") else deps).append(entry)
    return {"source": {"commit_hint": tag if re.fullmatch(r"[0-9a-f]{7,40}", tag or "") else None,
                       "how": "镜像 tag 是线上那次提交号，开发按它在业务仓库定位" if re.fullmatch(r"[0-9a-f]{7,40}", tag or "")
                              else f"镜像 tag {tag} 不是提交号，按版本号/发布记录在业务仓库定位"},
            "dependency_changes": deps,
            "base_image": ({"packages": base, "advice": "升级基础镜像（或 Go 等运行时版本）到包含修复的最新构建后重新构建"}
                           if base else None),
            "note": "只定位问题，未读业务仓库；升级说明与破坏性变更可用 web_search 查该包修复版本的发布说明"}


def source_plan(item, allowed_project, own):
    """自研镜像：定位业务仓库与线上提交，读依赖清单，把每个要升级的包落到具体文件和行。"""
    src = source_repo.resolve(item["image"], allowed=allowed_project, own_registries=own)
    if "error" in src:
        return {"source": None, "note": f"未能读取业务仓库：{src['error']}；先按包名交给开发处理"}
    files = source_repo.read_files(src["project"], src["commit"],
                                   source_repo.dependency_files(src["project"], src["commit"]))
    changes, system_pkgs = [], []
    for pkg in item["fix_packages"]:
        locations = source_repo.locate(pkg["package"], files)
        if locations:
            changes.append({"package": pkg["package"], "current": pkg["version"], "fix_to": pkg["fixed"],
                            "severity": pkg["severity"], "locations": locations})
        else:
            system_pkgs.append(f"{pkg['package']} {pkg['version']} → {pkg['fixed']}")
    bases = []
    for b in source_repo.base_images(files):
        _reg, _path, tag, _ = components.parse_image(b["image"])
        repo = registry.hub_repo(b["image"], own)
        target, why = (registry.suggest(tag, registry.tags(repo, name_filter=str((components.parse_version(tag) or [""])[0])),
                                        keep=2 if repo.split("/")[-1] in registry.RUNTIMES else 1)
                       if repo and tag and "$" not in b["image"] else (None, "非 Docker Hub 或含变量，需人工确认"))
        bases.append({**b, "suggested": target, "why": why})
    return {"source": {"project": src["project"], "commit": (src["commit"] or "")[:12], "how": src["how"],
                       "files_read": sorted(files)},
            "dependency_changes": changes,
            "base_image": ({"system_packages": system_pkgs, "from_lines": bases,
                            "advice": "这些是基础镜像里的系统包：把 FROM 更新/固定到最新构建后重新构建镜像"}
                           if system_pkgs else None),
            "note": "只读：修改依赖文件后由 CI 构建，提 MR 需另行授权写权限"}


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
                                                              if components.parse_version(tag) else ""),
                                           keep=2 if repo.split("/")[-1] in registry.RUNTIMES else 1)
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
                               "reachability": it["reachability"], "fix_packages": it["fix_packages"][:4]})
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    third_party.sort(key=lambda x: (order[x["priority"]], -x["fixable_high_critical"]))
    self_built.sort(key=lambda x: order[x["priority"]])
    read_source = store.get_setting("source_repo_read") == "1"
    for item in self_built[:limit]:
        item.update(source_plan(item, allowed_project, own) if read_source else locate_plan(item))
    return {"third_party": third_party[:limit], "third_party_total": len(third_party),
            "self_built": self_built[:limit], "self_built_total": len(self_built),
            "gitops_repos": [p for _n, p in projects],
            "note": "只出方案，不做修改；修改需经 code_* 工作区、静态校验与审批后提 MR，合并后由 ArgoCD 同步。"}
