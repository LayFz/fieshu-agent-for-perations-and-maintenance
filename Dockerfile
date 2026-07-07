FROM python:3.12-slim

WORKDIR /code
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
