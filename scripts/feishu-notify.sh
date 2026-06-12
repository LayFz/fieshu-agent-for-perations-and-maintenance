#!/usr/bin/env bash
# 飞书机器人通知（CI 用）。用法：feishu-notify.sh <status> <阶段名>
#   status: success/passed | failed/failure | 其它($CI_JOB_STATUS)
# 依赖：jq、curl（build runner 上有公网出口）。未配 FEISHU_WEBHOOK 则静默跳过，绝不让流水线失败。
set -uo pipefail
STATUS="${1:-unknown}"
PHASE="${2:-CI}"

[ -z "${FEISHU_WEBHOOK:-}" ] && { echo "未配 FEISHU_WEBHOOK，跳过通知"; exit 0; }

case "$STATUS" in
  success|passed)  color=green; word="成功 ✅" ;;
  failed|failure)  color=red;   word="失败 ❌" ;;
  *)               color=blue;  word="$STATUS 🚀" ;;
esac

title="飞书 Agent · ${PHASE}${word}"
content="**项目**：${CI_PROJECT_PATH:-feishu-agent}
**分支**：${CI_COMMIT_REF_NAME:-?}
**提交**：\`${CI_COMMIT_SHORT_SHA:-?}\`  ${CI_COMMIT_TITLE:-}
**触发**：${GITLAB_USER_NAME:-${GITLAB_USER_LOGIN:-?}} / ${CI_PIPELINE_SOURCE:-?}"
url="${CI_PIPELINE_URL:-}"

body=$(jq -n --arg t "$title" --arg c "$color" --arg md "$content" --arg url "$url" '
{
  msg_type: "interactive",
  card: {
    config: { wide_screen_mode: true },
    header: { template: $c, title: { tag: "plain_text", content: $t } },
    elements: ([ { tag: "div", text: { tag: "lark_md", content: $md } } ]
      + (if $url == "" then []
         else [ { tag: "action", actions: [ { tag: "button",
                 text: { tag: "plain_text", content: "查看 Pipeline" },
                 type: "primary", url: $url } ] } ] end))
  }
}')

# 可选：飞书自定义机器人签名校验（开了才传）
if [ -n "${FEISHU_SECRET:-}" ]; then
  ts=$(date +%s)
  sign=$(printf '' | openssl dgst -sha256 -hmac "$(printf '%s\n%s' "$ts" "$FEISHU_SECRET")" -binary | base64)
  body=$(printf '%s' "$body" | jq --arg ts "$ts" --arg sign "$sign" '. + {timestamp:$ts, sign:$sign}')
fi

curl -s -m 10 -H "Content-Type: application/json" -d "$body" "$FEISHU_WEBHOOK" >/dev/null || true
echo "已发送飞书通知：${PHASE}${word}"
