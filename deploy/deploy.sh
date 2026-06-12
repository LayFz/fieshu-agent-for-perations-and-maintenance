#!/usr/bin/env bash
# feishu-agent 内网(.51)部署/回滚脚本。在 .51 的部署目录执行（CI 远程调用，或手动）。
#
#   部署：REGISTRY=10.10.0.1 PROJECT=feishu-agent TAG=<sha> HARBOR_USER=.. HARBOR_PASSWORD=.. bash deploy.sh
#   回滚：bash deploy.sh rollback        # 切回上一个成功部署的 tag
#
# 前置（.51 一次性准备好，不入库）：
#   - config.yaml（飞书 app_id/app_secret + 初始管理员密码）
#   - data/（SQLite 持久化目录，首次自动建）
#   - /etc/docker/daemon.json 已把 10.10.0.1 加入 insecure-registries（.51 已就绪）
set -euo pipefail
cd "$(dirname "$0")"
ACTION="${1:-deploy}"
HEALTH_CONTAINER="${HEALTH_CONTAINER:-feishu-agent}"

: "${REGISTRY:?需要 REGISTRY，如 10.10.0.1}"
: "${PROJECT:?需要 PROJECT，如 feishu-agent}"

if [ "$ACTION" = "rollback" ]; then
  [ -f .prev_tag ] || { echo "✗ 无上一版本可回滚"; exit 1; }
  TAG="$(cat .prev_tag)"
  echo "↩ 回滚到 tag=$TAG"
else
  : "${TAG:?需要 TAG（镜像版本，通常是 commit sha）}"
fi
export REGISTRY PROJECT TAG

# 登录 Harbor（CI 传入凭证；手动执行可先 docker login 好）
if [ -n "${HARBOR_PASSWORD:-}" ] && [ -n "${HARBOR_USER:-}" ]; then
  echo "$HARBOR_PASSWORD" | docker login "$REGISTRY" -u "$HARBOR_USER" --password-stdin
fi

echo "▶ 拉取镜像 tag=$TAG ..."
docker compose -f compose.yml pull

# 部署前把当前版本存为上一版，便于回滚
if [ "$ACTION" = "deploy" ] && [ -f .cur_tag ]; then cp .cur_tag .prev_tag; fi

echo "▶ 启动/更新容器 ..."
docker compose -f compose.yml up -d --remove-orphans
echo "$TAG" > .cur_tag

echo "▶ 等待 $HEALTH_CONTAINER 健康 ..."
ok=0
for _ in $(seq 1 40); do
  s="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}nohealth{{end}}' "$HEALTH_CONTAINER" 2>/dev/null || echo none)"
  if [ "$s" = "healthy" ]; then ok=1; break; fi
  sleep 3
done
docker compose -f compose.yml ps
if [ "$ok" != "1" ]; then
  echo "✗ $HEALTH_CONTAINER 未达健康，请查日志：docker logs $HEALTH_CONTAINER"
  exit 1
fi
echo "✓ 部署完成 tag=$TAG（健康门：$HEALTH_CONTAINER 已就绪），后台 http://192.168.110.51:8800"
