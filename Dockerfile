FROM python:3.12-slim

WORKDIR /code
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# kubectl 二进制（给 kubectl 工具用，直连集群 API）。版本与集群 v1.36 对齐（skew ≤1 minor）。
# 注：kubectl 是二进制，不进 pip；随镜像走。amd64 构建；换 arch 改 ARCH 即可。
# kubectl 二进制（给 kubectl 工具用，直连集群 API）。版本与集群 v1.36 对齐（skew ≤1 minor）。
# 用 base 镜像自带的 python+ca 证书直接下载，不装 apt/curl——避免构建时 apt-get 打 Debian 公网镜像卡死
# （构建机不走出海代理时 apt-get update 会挂）。只碰 dl.k8s.io（构建机可直连）。amd64；换 arch 改 KUBECTL_ARCH。
ARG KUBECTL_VERSION=v1.36.2
ARG KUBECTL_ARCH=amd64
RUN python -c "import urllib.request; urllib.request.urlretrieve('https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${KUBECTL_ARCH}/kubectl', '/usr/local/bin/kubectl')" \
    && chmod +x /usr/local/bin/kubectl \
    && kubectl version --client

COPY app ./app
COPY core ./core
COPY feishu ./feishu
COPY llm ./llm
COPY obs ./obs
COPY ops ./ops
COPY sched ./sched
COPY web ./web

EXPOSE 8800
# 记忆/设置/用量(SQLite) 持久化（注释必须独占一行：Docker 不支持指令行尾内联注释，
# 否则注释会被当成 VOLUME 的额外参数，建出 '#' 等垃圾匿名卷，导致 compose recreate 失败）
VOLUME ["/code/data"]

# 健康检查：管理后台 /healthz（部署脚本据此判健康门 / 回滚）
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8800/healthz',timeout=3).status==200 else 1)"

# 管理后台(主线程) + 飞书长连接 bot(后台线程)，同进程
CMD ["python", "-m", "app.main"]
