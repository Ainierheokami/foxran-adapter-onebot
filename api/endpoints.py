import time
import asyncio
import re
from typing import Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Depends, Request

from app.api.core import get_or_create_session_context, active_processors
import app.api.core as api_core
from app.tasks.core.session_processor import SessionProcessor
from app.api.models.message_models import OneBotEvent
from app.api.endpoints.auth import require_auth
from app.adapters.message_protocol import bind_platform_id, make_user_message
from app.adapters.onebot_v11.config import onebot_v11_config


router = APIRouter()


def _public_account(account: Dict[str, Any], status: Dict[str, Any], reverse_count: int) -> Dict[str, Any]:
    result = dict(account)
    result["access_token"] = ""
    result["access_token_set"] = bool(account.get("access_token"))
    mode = str(account.get("connection_mode") or "forward")
    if reverse_count:
        state = "connected"
    elif not account.get("enabled"):
        state = "disabled"
    elif mode == "reverse":
        state = "waiting"
    else:
        state = str(status.get("state") or "stopped")
    result["status"] = {
        **status,
        "state": state,
        "reverse_connections": reverse_count,
    }
    result["reverse_ws_path"] = f"/onebot/v11/ws/{account['id']}"
    return result


@router.get("/api/adapters/onebot_v11/accounts")
async def get_onebot_accounts(_: bool = Depends(require_auth)):
    from app.adapters.onebot_v11.network.client import onebot_v11_client
    from app.adapters.onebot_v11.network.reverse_ws import reverse_connection_status

    statuses = onebot_v11_client.status()
    reverse = reverse_connection_status()
    accounts = onebot_v11_config.get_accounts(force_reload=True)
    return {
        "accounts": [
            _public_account(account, statuses.get(str(account["id"]), {}), reverse.get(str(account["id"]), 0))
            for account in accounts
        ]
    }


@router.put("/api/adapters/onebot_v11/accounts")
async def put_onebot_accounts(request: Request, _: bool = Depends(require_auth)):
    body = await request.json()
    accounts = body.get("accounts") if isinstance(body, dict) else None
    if not isinstance(accounts, list) or not accounts:
        raise HTTPException(status_code=400, detail="至少需要一个 OneBot 账户")
    existing = {str(item["id"]): item for item in onebot_v11_config.get_accounts(force_reload=True)}
    cleaned: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in accounts:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="账户配置格式错误")
        account_id = str(raw.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account_id) or account_id in seen:
            raise HTTPException(status_code=400, detail=f"账户 ID 无效或重复: {account_id}")
        seen.add(account_id)
        account = dict(raw)
        account.pop("status", None)
        account.pop("access_token_set", None)
        account.pop("reverse_ws_path", None)
        if not str(account.get("access_token") or ""):
            account["access_token"] = str(existing.get(account_id, {}).get("access_token") or "")
        mode = str(account.get("connection_mode") or "forward")
        if mode not in {"forward", "reverse", "both"}:
            raise HTTPException(status_code=400, detail=f"不支持的 connection_mode: {mode}")
        cleaned.append(account)
    from app.adapters.onebot_v11.network.client import onebot_v11_client

    await onebot_v11_client.stop()
    onebot_v11_config.save_accounts(cleaned)
    await onebot_v11_client.start()
    return await get_onebot_accounts(True)


@router.post("/api/adapters/onebot_v11/reload")
async def reload_onebot_accounts(_: bool = Depends(require_auth)):
    from app.adapters.onebot_v11.network.client import onebot_v11_client

    await onebot_v11_client.stop()
    onebot_v11_config.get_config(force_reload=True)
    await onebot_v11_client.start()
    return await get_onebot_accounts(True)


@router.post("/api/onebot/v11/event")
async def onebot_v11_event(event: OneBotEvent, wait_for_reply: bool = False, timeout_seconds: float = 8.0, _: bool = Depends(require_auth)):
    if (event.post_type or "message").lower() != "message":
        return {"status": "ignored", "reason": "non-message event"}

    # 获取消息处理器
    try:
        from app.data_mappers import get_message_processor
        message_processor = get_message_processor()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"消息处理器初始化失败: {e}")

    platform = "onebot"
    content = event.raw_message or event.message or ""
    user_id = str(event.user_id or "onebot_user")
    user_name = (event.sender or {}).get("nickname") or "OneBot User"

    # 获取/创建会话
    try:
        session_ctx = await get_or_create_session_context(
            session_id=None,
            user_id=user_id,
            user_name=user_name,
            platform=platform,
            bot_name=None,
            history_dicts=None,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建会话上下文失败: {e}")

    # 确保会话处理器已启动
    if session_ctx.session_id not in active_processors:
        if not api_core.core_agent:
            raise HTTPException(status_code=503, detail="AI核心未初始化")
        try:
            processor = SessionProcessor(session_ctx, api_core.core_agent)
            await processor.start()
            active_processors[session_ctx.session_id] = processor
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"会话处理器启动失败: {e}")

    # 统一处理
    try:
        processed = await message_processor.process_incoming_message(
            platform=platform,
            raw_content=content,
            message_data={
                "role": "user",
                "content": content,
                "user_id": user_id,
                "user_name": user_name,
                "platform_id": event.message_id,
            },
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"入站消息处理失败: {e}")

    current_message = make_user_message(
        content=processed.internal,
        user_id=user_id,
        user_name=user_name,
        platform=platform,
        platform_id=event.message_id,
        raw_content=content,
    )
    bind_platform_id(session_ctx, current_message, event.message_id)
    await session_ctx.handle_new_message(current_message)

    reply_text: Optional[str] = None
    if wait_for_reply:
        start_time = time.time()
        deadline = start_time + max(0.5, timeout_seconds)
        while time.time() < deadline:
            history = session_ctx.get_history()
            if history:
                for msg in reversed(history):
                    if msg.role == "assistant" and msg.timestamp >= start_time:
                        try:
                            reply_text = await message_processor.process_outgoing_message(
                                response=msg.content, platform=platform
                            )
                        except Exception:
                            reply_text = msg.content
                        break
            if reply_text:
                break
            await asyncio.sleep(0.2)

    return {
        "status": "ok" if not wait_for_reply or reply_text is not None else "pending",
        "session_id": session_ctx.session_id,
        "message_id": current_message.message_id,
        "platform_id": current_message.platform_id,
        "reply": reply_text,
    }


