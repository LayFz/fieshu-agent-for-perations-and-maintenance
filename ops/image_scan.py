"""镜像扫描结果采集：对资产清单里来自自家 Harbor 的镜像（自研镜像与经 Harbor 代理缓存的公共镜像），
读取 Harbor 的扫描概览与漏洞明细，存入 image_scans / image_vulns。优先用运行时 digest 定位制品，没有再用 tag。
"""
import time

from core import assetstore, components, store
from . import asset_scan, harbor

MAX_IMAGES = 200


def _harbor_ref(asset, own):
    """资产 → (project, repo, reference)；不是自家 Harbor 的镜像返回 None。"""
    registry, path, tag, digest = components.parse_image(asset.get("image") or "")
    if not registry or not any(registry == r or registry.endswith("." + r) for r in own):
        return None
    project, _, repo = path.partition("/")
    if not repo:
        return None
    ref = asset.get("digest") or digest or tag or "latest"
    return project, repo, ref


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
    for (project, repo, reference), image in list(targets.items())[:MAX_IMAGES]:
        art = harbor.artifact(project, repo, reference)
        if "error" in art:
            errors += 1
            scans.append({"project": project, "repo": repo, "reference": reference, "image": image,
                          "status": "error", "error": art["error"]})
            continue
        ov = harbor.scan_overview(art)
        item = {"project": project, "repo": repo, "reference": reference, "image": image, **ov}
        if ov["status"].lower() == "success":
            detail = harbor.vulnerabilities(project, repo, reference)
            if "error" in detail:
                item["error"] = detail["error"]
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
