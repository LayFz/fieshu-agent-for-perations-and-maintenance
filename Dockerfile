FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY feishu_agent ./feishu_agent

EXPOSE 8800
VOLUME ["/app/data"]          # 记忆/设置/用量(SQLite) 持久化

# 管理后台(主线程) + 飞书长连接 bot(后台线程)，同进程
CMD ["python", "-m", "feishu_agent.main"]
