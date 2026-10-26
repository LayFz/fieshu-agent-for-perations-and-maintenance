"""组件目录：把镜像引用归一成「组件 + 版本」，并给出在漏洞情报里识别该组件的关键词。

资产采集（ops.asset_scan）和漏洞匹配（sec.match）共用这一份，保证两边对「是不是同一个东西」的判断一致。
关键词按「包名/仓库路径」优先：GHSA/OSV 情报的产品字段是 ecosystem:package（如 go:github.com/argoproj/argo-cd/v3），
最可靠；产品自由文本（如 KEV 的 "Kubernetes ingress-nginx"）次之。
"""
import re

# 组件 → 识别它的关键词（小写，按词边界匹配）
CATALOG = {
    "argocd": ["argoproj/argo-cd", "argo-cd", "argo cd", "argocd"],
    "argocd-image-updater": ["argocd-image-updater", "argo cd image updater"],
    "ingress-nginx": ["ingress-nginx", "ingress nginx", "kubernetes/ingress-nginx"],
    "kubernetes": ["kubernetes", "k8s.io/kubernetes", "kube-apiserver", "kubelet", "kube-proxy"],
    "k3s": ["k3s-io/k3s", "k3s"],
    "containerd": ["containerd/containerd", "containerd"],
    "runc": ["opencontainers/runc", "runc"],
    "linux-kernel": ["linux kernel", "linux:linux"],
    "coredns": ["coredns"],
    "traefik": ["traefik/traefik", "traefik"],
    "metallb": ["metallb"],
    "cloudnative-pg": ["cloudnative-pg", "cloudnativepg"],
    "postgresql": ["postgresql", "postgres"],
    "redis": ["redis"],
    "rabbitmq": ["rabbitmq"],
    "minio": ["minio/minio", "minio"],
    "sealed-secrets": ["bitnami-labs/sealed-secrets", "sealed-secrets", "sealed secrets"],
    "buildkit": ["moby/buildkit", "buildkit"],
    "gitlab": ["gitlab"],
    "gitlab-runner": ["gitlab-runner", "gitlab runner"],
    "harbor": ["goharbor/harbor", "harbor"],
    "casdoor": ["casdoor"],
    "openobserve": ["openobserve"],
    "vector": ["vectordotdev/vector", "timberio/vector"],
    "headlamp": ["headlamp"],
    "nginx": ["nginx"],
    "grafana": ["grafana"],
    "prometheus": ["prometheus"],
    "node-exporter": ["node_exporter", "node-exporter"],
    "local-path-provisioner": ["local-path-provisioner"],
    "metrics-server": ["metrics-server"],
}

# 镜像仓库路径 → 组件（按顺序匹配；未命中时用路径最后一段）
_IMAGE_RULES = [
    (r"argoproj/argocd-image-updater|argocd-image-updater", "argocd-image-updater"),
    (r"argoproj/argocd|argoproj/argo-cd|/argocd$", "argocd"),
    (r"ingress-nginx/(controller|kube-webhook-certgen)", "ingress-nginx"),
    (r"coredns", "coredns"),
    (r"traefik", "traefik"),
    (r"metallb", "metallb"),
    (r"cloudnative-pg/cloudnative-pg|cloudnative-pg/postgresql-operator", "cloudnative-pg"),
    (r"cloudnative-pg/postgresql|(^|/)postgres(ql)?$|bitnami/postgresql", "postgresql"),
    (r"(^|/)redis(-stack)?$|bitnami/redis", "redis"),
    (r"(^|/)rabbitmq$|bitnami/rabbitmq", "rabbitmq"),
    (r"(^|/)minio$", "minio"),
    (r"sealed-secrets", "sealed-secrets"),
    (r"moby/buildkit", "buildkit"),
    (r"gitlab-runner", "gitlab-runner"),
    (r"casdoor", "casdoor"),
    (r"openobserve", "openobserve"),
    (r"timberio/vector|vectordotdev/vector", "vector"),
    (r"headlamp", "headlamp"),
    (r"local-path-provisioner", "local-path-provisioner"),
    (r"metrics-server", "metrics-server"),
    (r"node-exporter|node_exporter", "node-exporter"),
    (r"(^|/)nginx(-unprivileged)?$", "nginx"),
    (r"grafana", "grafana"),
    (r"prometheus", "prometheus"),
]
_GENERIC_TAIL = {"controller", "server", "api", "app", "web", "worker", "manager", "operator", "agent", "backend",
                 "frontend", "service", "core"}
_SEMVER = re.compile(r"^v?(\d+(?:\.\d+){0,3})")


def parse_image(ref):
    """'registry/path/name:tag@sha256:…' → (registry, path, tag, digest)。"""
    ref = (ref or "").strip()
    digest = ""
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    tag = ""
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        ref, tag = ref.rsplit(":", 1)
    parts = ref.split("/")
    registry = parts[0] if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost") else ""
    path = "/".join(parts[1:] if registry else parts)
    return registry, path, tag, digest


def version_of(tag):
    """从 tag 里取版本：v1.11.3 → 1.11.3；3-management-alpine → 3；8 位提交号或 latest → ''。"""
    if not tag or tag == "latest" or re.fullmatch(r"[0-9a-f]{7,40}", tag):
        return ""
    m = _SEMVER.match(tag)
    return m.group(1) if m else ""


PROXY_PROJECTS = ("dockerhub", "docker.io", "ghcr", "quay", "gcr", "k8s", "registry.k8s.io", "proxy", "mirror",
                  "cache", "infra")


def component_of(ref, own_registries=(), proxy_projects=PROXY_PROJECTS):
    """返回 (组件, 版本, 是否自研)。自研镜像（自家 Harbor 项目）组件名用镜像名，依赖漏洞要靠镜像扫描。
    自家 Harbor 的代理缓存/镜像项目（如 harbor/dockerhub/library/mysql）按它代理的公共镜像处理。"""
    registry, path, tag, _ = parse_image(ref)
    if registry and any(registry == r or registry.endswith("." + r) for r in own_registries):
        project, _, rest = path.partition("/")
        if project.lower() in proxy_projects and rest:
            registry, path = "", rest[len("library/"):] if rest.startswith("library/") else rest
    low = path.lower()
    for pattern, name in _IMAGE_RULES:
        if re.search(pattern, low):
            return name, version_of(tag), False
    own = bool(registry) and any(registry == r or registry.endswith("." + r) for r in own_registries)
    segs = [s for s in low.split("/") if s]
    name = segs[-1] if segs else low
    if name in _GENERIC_TAIL and len(segs) > 1:
        name = segs[-2]
    name = re.sub(r"^mirrored-", "", name)
    return name, version_of(tag), own


# 同一厂商名下单独安装的子产品：公告针对它们时，版本号不属于组件本体（"GitLab AI Gateway 19.2.4" ≠ GitLab 19.2.4）
SUBPRODUCTS = {
    "gitlab": ["ai gateway", "runner", "pages", "workhorse", "agent server", "kas", "duo", "omnibus"],
    "argocd": ["image updater", "rollouts", "workflows", "events"],
    "kubernetes": ["dashboard", "csi", "ingress", "gateway api"],
    "redis": ["insight", "enterprise", "stack"],
    "grafana": ["agent", "loki", "tempo", "mimir", "oncall"],
}


def subproduct(component, text):
    """公告文本是否针对该组件的子产品；返回子产品名或 None。只看「关键词 + 子产品」紧邻出现。"""
    low = (text or "").lower()
    for kw in keywords(component):
        for sub in SUBPRODUCTS.get(component, []):
            if re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"[\s-]+(self-hosted\s+)?" + re.escape(sub) + r"(?![a-z0-9])", low):
                return sub
    return None


def keywords(component):
    return CATALOG.get(component, [component])


def mentions(component, text):
    """情报文本里是否提到该组件；返回命中的关键词或 None。按词边界匹配，避免 redis 命中 redistribute。"""
    low = (text or "").lower()
    for kw in keywords(component):
        if re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", low):
            return kw
    return None


def parse_version(v):
    m = _SEMVER.match((v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


# 更具体的组件被提到时，压掉它所包含的泛称（"Kubernetes ingress-nginx" 是 ingress-nginx 的漏洞，不是 Kubernetes 本身）
SUPERSEDES = {
    "ingress-nginx": {"nginx", "kubernetes"},
    "argocd-image-updater": {"argocd"},
    "gitlab-runner": {"gitlab"},
    "cloudnative-pg": {"postgresql", "kubernetes"},
    "k3s": {"kubernetes"},
    "coredns": {"kubernetes"},
    "metrics-server": {"kubernetes"},
    "sealed-secrets": {"kubernetes"},
    "metallb": {"kubernetes"},
}


def mentioned(components, text):
    """在文本里找出提到的组件 {组件: 命中关键词}，并按 SUPERSEDES 去掉被更具体组件覆盖的泛称。"""
    hits = {c: kw for c in components if (kw := mentions(c, text))}
    for specific, generic in SUPERSEDES.items():
        if specific in hits:
            for g in generic:
                hits.pop(g, None)
    return hits


# 组件本身的源码包（GHSA/OSV 产品字段 ecosystem:package）。带包名的情报只按这里精确匹配，
# 不再做关键词匹配：pip:redis 是 Python 客户端，不是 Redis 服务。
PACKAGES = {
    "argocd": [r"go:github\.com/argoproj/argo-cd(/v\d+)?$"],
    "argocd-image-updater": [r"go:github\.com/argoproj-labs/argocd-image-updater(/v\d+)?$"],
    "ingress-nginx": [r"go:k8s\.io/ingress-nginx$"],
    "kubernetes": [r"go:k8s\.io/kubernetes$"],
    "k3s": [r"go:github\.com/k3s-io/k3s$"],
    "containerd": [r"go:github\.com/containerd/containerd(/v\d+)?$"],
    "runc": [r"go:github\.com/opencontainers/runc$"],
    "coredns": [r"go:github\.com/coredns/coredns$"],
    "traefik": [r"go:github\.com/traefik/traefik(/v\d+)?$"],
    "metallb": [r"go:go\.universe\.tf/metallb$"],
    "cloudnative-pg": [r"go:github\.com/cloudnative-pg/cloudnative-pg$"],
    "minio": [r"go:github\.com/minio/minio$"],
    "sealed-secrets": [r"go:github\.com/bitnami-labs/sealed-secrets$"],
    "buildkit": [r"go:github\.com/moby/buildkit$"],
    "gitlab-runner": [r"go:gitlab\.com/gitlab-org/gitlab-runner$"],
    "harbor": [r"go:github\.com/goharbor/harbor$"],
    "casdoor": [r"go:github\.com/casdoor/casdoor$"],
    "headlamp": [r"go:github\.com/(kubernetes-sigs|headlamp-k8s)/headlamp.*$"],
}
_ECOSYSTEM = re.compile(r"^(go|npm|pip|pypi|maven|rust|crates\.io|composer|nuget|rubygems|gem|hex|pub|swift):", re.I)


def is_package(product):
    return bool(_ECOSYSTEM.match(product or ""))


def package_component(product, components):
    low = (product or "").lower()
    for comp in components:
        if any(re.match(p, low) for p in PACKAGES.get(comp, [])):
            return comp
    return None


_OS_VERSION = re.compile(r"-r\d+$|deb\d|ubuntu|\+b\d|^\d+:|el\d|-\d+(\.\d+)*$")


def package_kind(package, version=""):
    """Trivy 包名/版本 → 包类型：runtime（Go stdlib 等运行时）/ system（基础镜像系统包）/
    go / npm / composer / maven / app（其他应用依赖）。"""
    name = package or ""
    if name == "stdlib":
        return "runtime"
    if name.startswith("@"):
        return "npm"
    if "/" in name:
        if "." in name.split("/", 1)[0]:
            return "go"
        if re.fullmatch(r"[\w.-]+/[\w.-]+", name):
            return "composer"
    if re.search(r"^[\w.-]+:[\w.-]+$", name):
        return "maven"
    if _OS_VERSION.search(version or ""):
        return "system"
    return "app"


def is_base_layer(package, version=""):
    """基础镜像层（系统包、语言运行时）：更新基础镜像并重建即可修复，不需要改业务依赖。"""
    return package_kind(package, version) in ("system", "runtime")
