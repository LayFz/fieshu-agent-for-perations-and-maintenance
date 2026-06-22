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
COPY sched ./sched
COPY web ./web

EXPOSE 8800
VOLUME ["/code/data"]         # 记忆/设置/用量(SQLite) 持久化

# 健康检查：管理后台 /healthz（部署脚本据此判健康门 / 回滚）
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8800/healthz',timeout=3).status==200 else 1)"

# 管理后台(主线程) + 飞书长连接 bot(后台线程)，同进程
CMD ["python", "-m", "app.main"]
