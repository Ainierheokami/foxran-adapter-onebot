from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.logger import setup_logger
from app.adapters.onebot_v11.config import onebot_v11_config
from app.adapters.onebot_v11.handlers import handle_onebot_event
from app.adapters.onebot_v11.senders import ReverseOneBotReplySender, parse_onebot_echo
from app.adapters.onebot_v11.action_tracker import onebot_action_tracker
from app.api.core import active_sessions


logger = setup_logger(__name__)
router = APIRouter()


def _get_cookie_value(websocket: WebSocket, key: str) -> str:
    try:
        cookies = websocket.cookies
        if key in cookies:
            return cookies.get(key) or ""
    except Exception:
        pass
    raw = websocket.headers.get("cookie") or ""
    if not raw:
        return ""
    parts = [p.strip() for p in raw.split(";") if "=" in p]
    for part in parts:
        name, value = part.split("=", 1)
        if name.strip() == key:
            return value.strip()
    return ""


def _extract_token(websocket: WebSocket) -> str:
    auth = websocket.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    header_token = websocket.headers.get("x-access-token")
    if header_token:
        return header_token.strip()
    return _get_cookie_value(websocket, "access_token")


def _auth_ok(websocket: WebSocket) -> bool:
    cfg = onebot_v11_config.get_config()
    expected = (cfg.get("access_token") or "").strip()
    if not expected:
        return False
    return _extract_token(websocket) == expected


@router.websocket("/onebot/v11/ws")
async def onebot_v11_reverse_ws(websocket: WebSocket):
    await websocket.accept()

    cfg = onebot_v11_config.get_config(force_reload=True)
    mode = str(cfg.get("connection_mode", "forward")).strip().lower()
    enabled = bool(cfg.get("enabled", False))
    if not enabled or mode not in ("reverse", "both"):
        logger.warning(
            "OneBot 反向 WS 拒绝：enabled=%s, connection_mode=%s",
            enabled,
            mode,
        )
        await websocket.send_json({"error": "reverse mode disabled"})
        await websocket.close()
        return

    if not _auth_ok(websocket):
        logger.warning("OneBot 反向 WS 拒绝：鉴权失败")
        await websocket.send_json({"error": "unauthorized"})
        await websocket.close()
        return

    logger.info("OneBot v11 反向 WS 已连接")

    sender_factory = lambda ctx: ReverseOneBotReplySender(websocket, ctx)

    try:
        while True:
            try:
                data = await websocket.receive_json()
            except WebSocketDisconnect:
                logger.info("OneBot v11 反向 WS 客户端主动断开")
                break
            except Exception as e:
                logger.warning(f"反向 WS 接收异常: {e}", exc_info=True)
                continue

            if not isinstance(data, dict):
                continue

            if "post_type" in data:
                await handle_onebot_event(data, sender_factory=sender_factory)
                continue

            if "status" in data and "echo" in data:
                logger.debug(f"OneBot 动作响应: {data.get('status')}, echo={data.get('echo')}")
                onebot_action_tracker.resolve(data.get("echo"), data)
                session_id, outgoing_message_id = parse_onebot_echo(data.get("echo"))
                platform_message_id = None
                data_block = data.get("data")
                if isinstance(data_block, dict):
                    platform_message_id = data_block.get("message_id")
                if session_id and outgoing_message_id and platform_message_id is not None:
                    session_ctx = active_sessions.get(session_id)
                    if session_ctx:
                        session_ctx.set_platform_id_for_message(outgoing_message_id, platform_message_id)

    finally:
        logger.info("OneBot v11 反向 WS 已断开")

