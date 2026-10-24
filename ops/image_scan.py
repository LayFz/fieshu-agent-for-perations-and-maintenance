"""镜像扫描结果采集：对资产清单里来自自家 Harbor 的镜像（自研镜像与经 Harbor 代理缓存的公共镜像），
读取 Harbor 的扫描概览与漏洞明细，存入 image_scans / image_vulns。优先用运行时 digest 定位制品，没有再用 tag。
"""
import time

from core import assetstore, components, store
from . import asset_scan, harbor

MAX_IMAGES = 200


def _harbor_ref(asset, own):
    """资产 → (project, repo, 运行时 digest, tag)；不是自家 Harbor 的镜像返回 None。"""
    registry, path, tag, digest = components.parse_image(asset.get("image") or "")
    if not registry or not any(registry == r or registry.endswith("." + r) for r in own):
        return None
    project, _, repo = path.partition("/")
    if not repo:
        return None
    return project, repo, asset.get("digest") or digest or "", tag or ("" if digest else "latest")


def _locate(project, repo, digest, tag):
    """先按运行时 digest 找制品；找不到再按 tag（代理缓存会刷新同一 tag 的制品，此时不是运行中的那一版）。
    返回 (制品或错误, 实际 reference, 匹配说明)。"""
    if digest:
        art = harbor.artifact(project, repo, digest)
        if "error" not in art or not art.get("not_found"):
            return art, digest, ""
    if tag:
        art = harbor.artifact(project, repo, tag)
        if "error" not in art:
            note = "按 tag 匹配：Harbor 里该 tag 的制品与运行中的 digest 不同（镜像已更新，线上可能是旧版）" if digest else ""
            return art, tag, note
        if not art.get("not_found"):
            return art, tag, ""
    return {"error": "Harbor 里已没有这个镜像（代理缓存被清理或从未经 Harbor 拉取）", "not_found": True}, digest or tag, ""


def collect():
    if not harbor.configured():
        return {"status": "skipped", "error": "Harbor 未配置"}
    own = asset_scan.own_registries()
    targets = {}
    for a in assetstore.list_assets(limit=20000):
        ref = _harbor_ref(a, own)
        if ref and ref not in targets:
            targets[ref] = a["image"]
    scans, errors = [], 0
    for (project, repo, digest, tag), image in list(targets.items())[:MAX_IMAGES]:
        art, reference, note = _locate(project, repo, digest, tag)
        if "error" in art:
            missing = art.get("not_found")
            errors += 0 if missing else 1
            scans.append({"project": project, "repo": repo, "reference": reference, "image": image,
                          "status": "not_in_harbor" if missing else "error", "error": art["error"]})
            continue
        ov = harbor.scan_overview(art)
        item = {"project": project, "repo": repo, "reference": reference, "image": image, **ov, "error": note}
        if ov["status"].lower() == "success":
            detail = harbor.vulnerabilities(project, repo, reference)
            if "error" in detail:
                item["error"] = detail["error"]
                errors += 1
            else:
                item["vulns"] = detail["vulnerabilities"]
        elif ov["status"] == "not_scanned" and harbor.config()["trigger_scan"]:
            harbor.trigger_scan(project, repo, reference)
            item["status"] = "scan_requested"
        scans.append(item)
    assetstore.replace_scans(scans)
    store.set_setting("image_scan_at", str(time.time()))
    done = sum(1 for s in scans if (s.get("status") or "").lower() == "success")
    return {"status": "ok" if not errors else "partial", "images": len(scans), "scanned": done, "errors": errors,
            "truncated": len(targets) > MAX_IMAGES}
