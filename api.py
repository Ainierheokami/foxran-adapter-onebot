import time
import asyncio
from typing import Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Depends

from app.api.core import get_or_create_session_context, active_processors
import app.api.core as api_core
from app.tasks.core.session_processor import SessionProcessor
from app.api.models.message_models import OneBotEvent
from app.api.endpoints.auth import require_auth
from app.adapters.message_protocol import bind_platform_id, make_user_message


router = APIRouter()


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


