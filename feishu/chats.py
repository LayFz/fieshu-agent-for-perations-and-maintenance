"""列出应用当前加入的群；权限不足显式返回，不把历史会话冒充群列表。"""
import requests

from feishu import auth


def list_for(app):
    token = auth.use_app(app.get("feishu_app_id"), app.get("feishu_app_secret"))
    try:
        bearer = auth.tenant_token()
        chats, cursor, seen = [], None, set()
        for _ in range(20):
            response = requests.get("https://open.feishu.cn/open-apis/im/v1/chats",
                headers={"Authorization": "Bearer " + bearer},
                params={"page_size": 100, "page_token": cursor}, timeout=15, allow_redirects=False)
            try:
                data = response.json()
            except ValueError:
                return {"chats": chats, "complete": False, "error": f"飞书 HTTP {response.status_code}，响应格式无效"}
            if data.get("code") == 99991672:
                return {"chats": chats, "complete": False, "code": 99991672,
                        "error": "应用缺少群信息读取权限；请在飞书开放平台开通 im:chat:readonly 或 im:chat.group_info:readonly 并发布"}
            if response.status_code != 200:
                return {"chats": chats, "complete": False, "code": data.get("code"),
                        "error": f"飞书 HTTP {response.status_code}；群列表暂不可用"}
            if data.get("code") != 0:
                return {"chats": chats, "complete": False, "code": data.get("code"),
                        "error": "飞书群列表读取失败；请检查应用权限及发布状态"}
            page = data.get("data") or {}
            chats.extend({"chat_id": row.get("chat_id"), "name": row.get("name")} for row in page.get("items") or [])
            if not page.get("has_more"):
                return {"chats": chats, "complete": True}
            cursor = page.get("page_token")
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
        return {"chats": chats, "complete": False, "error": "群列表分页达到上限"}
    except Exception as exc:
        return {"chats": [], "complete": False, "error": "飞书群列表暂不可用：" + type(exc).__name__}
    finally:
        auth.reset_ctx(token)
