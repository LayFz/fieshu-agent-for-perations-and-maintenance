# feishu-agent 部署手册（内网 .51 = ubantu2）

照支付中台的套路：**推 main → CI 构镜像 → Harbor(10.10.0.1) → SSH 部署 .51 → 飞书通知**。
feishu-agent 是**单容器**(Python + SQLite，前端内置)，比支付中台简单：没有 PG/Redis/多前端。

## 架构
- 1 个容器 `feishu-agent`：飞书机器人长连接(**只出站**) + 管理后台(`:8800`)。
- 数据：SQLite 单文件，持久化在 `/opt/feishu-agent/data/`（设置/记忆/用量/管理员，**部署不清**）。
- 访问：公司 LAN `http://192.168.110.51:8800` 进管理后台。
- 密钥：`/opt/feishu-agent/config.yaml`（飞书凭证 + 初始管理员密码），**仅在 .51，不入库不进镜像**。

## .51 现状（已就绪，无需再装）
Docker 29.1.3 + compose v2；`daemon.json` 已含 `insecure-registries:[10.10.0.1]`；
WireGuard `wg0=10.10.0.3`；Harbor 10.10.0.1 可达；:8800 空闲。

## 一次性准备

### 1) Harbor 建项目 + 机器人账号
- Harbor(10.10.0.1) 新建 **Private 项目 `feishu-agent`**。
- 建**项目级机器人** `robot$feishu-agent+ci`，权限只给 Repository **pull + push**，永不过期。
- 记下机器人名与 token（配进 GitLab 变量 HARBOR_USER / HARBOR_PASSWORD）。

### 2) .51 放密钥 + 数据目录
```bash
mkdir -p /opt/feishu-agent/data
cp config.example.yaml /opt/feishu-agent/config.yaml   # 填 app_secret + 强管理员密码 + session_secret
chmod 600 /opt/feishu-agent/config.yaml
```

### 3) infra runner(.18) → .51 免密
把 runner 的部署公钥 `id_ed25519_deploy.pub` 追加到 .51 的 `/root/.ssh/authorized_keys`。

### 4) GitLab 项目 Variables（Settings → CI/CD → Variables）
```
HARBOR_REGISTRY=10.10.0.1     HARBOR_PROJECT=feishu-agent
HARBOR_USER=robot$feishu-agent+ci    HARBOR_PASSWORD=<token>     (Masked)
DEPLOY_HOST=192.168.110.51    DEPLOY_DIR=/opt/feishu-agent
DEPLOY_SSH_KEY=/root/.ssh/id_ed25519_deploy
FEISHU_WEBHOOK=<群机器人 webhook>  (Masked)    FEISHU_SECRET=<可选>  (Masked)
```

## 部署
- **自动**：推 `main` → 流水线 build→deploy→notify 自动跑。
- **手动**（在 .51 部署目录）：
  ```bash
  REGISTRY=10.10.0.1 PROJECT=feishu-agent TAG=<sha> \
    HARBOR_USER=.. HARBOR_PASSWORD=.. bash deploy.sh
  ```

## 回滚
```bash
cd /opt/feishu-agent && bash deploy.sh rollback   # 切回上一个成功 tag
```

## 验证
```bash
curl -s http://192.168.110.51:8800/healthz        # {"ok":true}
docker logs -f feishu-agent                        # 看 bot 长连接 connected
```
浏览器开 `http://192.168.110.51:8800` 登录管理后台。
