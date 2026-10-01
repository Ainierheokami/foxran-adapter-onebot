from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Tuple
import json
from pathlib import Path
from urllib.parse import unquote, urlparse

from starlette.websockets import WebSocketState

from app.logger import setup_logger
from app.message import ExtensionSegment, File, MessageChain, Reply, Segment, segment_registry
from app.outbound import OutboundMessage


logger = setup_logger(__name__)


def _coerce_int(value: Any) -> Any:
    try:
        return int(value)
    except Exception:
        return value


def _file_name_from_source(source: str) -> str:
    try:
        parsed = urlparse(source)
        path = unquote(parsed.path) if parsed.scheme else source
        name = Path(path.replace("\\", "/")).name
        if name:
            return name
    except Exception:
        pass
    return "attachment"


class OneBotOutboundPort:
    """Foxran outbound port for OneBot v11 (core-refactor R3b).

    The transport is picked when sending: the account's forward client if it is
    connected, otherwise the account's latest reverse WebSocket. Echo isolation
    and outbound metrics are handled by the framework's ConversationSender.
    """

    def _transport(self, account_id: str):
        from app.adapters.onebot_v11.network.client import onebot_v11_client
        from app.adapters.onebot_v11.network.reverse_ws import reverse_socket_for

        client = onebot_v11_client.client_for(account_id)
        if client is not None and client.is_connected:
            return client.send_action
        websocket = reverse_socket_for(account_id)
        if websocket is not None and websocket.client_state == WebSocketState.CONNECTED:
            async def send_reverse(action: str, params: Dict[str, Any], echo: Optional[str] = None) -> bool:
                await websocket.send_json({"action": action, "params": params, "echo": echo or str(uuid.uuid4())})
                return True
            return send_reverse
        return None

    async def send_action(self, account_id: str, action: str, params: Dict[str, Any], echo: Optional[str] = None) -> bool:
        transport = self._transport(account_id)
        if transport is None:
            logger.warning(f"OneBot 动作发送失败（账号 {account_id} 未连接）: {action}")
            return False
        try:
            return bool(await transport(action, params, echo=echo))
        except Exception as e:
            logger.warning(f"OneBot 动作发送失败: {action}, 错误: {e}")
            return False

    def encode(self, session_ctx: Any, segments: MessageChain) -> OutboundMessage:
        """CQ text per message part; files become their own upload actions."""
        from app.adapters.onebot_v11.adapter import OneBotAdapter

        adapter = OneBotAdapter()
        parts: List[Tuple[str, Any]] = []
        run: List[Segment] = []

        def flush() -> None:
            if run:
                parts.append(("message", adapter.to_platform_format(MessageChain(list(run)))))
                run.clear()

        for seg in _flatten(segments):
            if isinstance(seg, Reply):
                seg = _with_platform_reply_id(session_ctx, seg)
            if isinstance(seg, File):
                flush()
                parts.append(("file", seg))
            else:
                run.append(seg)
        flush()
        text = "".join(adapter.to_platform_format(item) if kind == "file" else item for kind, item in parts)
        return OutboundMessage(text=text, payload=parts)

    async def deliver(self, session_ctx: Any, conversation: Any, message: OutboundMessage, message_id: Optional[str]) -> Optional[str]:
        echo = _build_echo(session_ctx.session_id, message_id) if message_id else None
        account_id = conversation.account_id
        if conversation.is_group:
            target = {"group_id": _coerce_int(conversation.id)}
            message_action, upload_action = "send_group_msg", "upload_group_file"
        else:
            target = {"user_id": _coerce_int(conversation.id)}
            message_action, upload_action = "send_private_msg", "upload_private_file"

        action_index = 0

        async def send(action: str, params: Dict[str, Any]) -> None:
            nonlocal action_index
            action_echo = echo if action_index == 0 else None
            action_index += 1
            await self.send_action(account_id, action, params, echo=action_echo)

        # Regular content and file uploads go through their own APIs; the
        # platform id arrives asynchronously with the action response (echo).
        for kind, item in message.payload:
            if kind == "message":
                if item.strip():
                    await send(message_action, {**target, "message": item})
                continue
            source = item.media.file or item.media.url
            if source:
                await send(upload_action, {**target, "file": source, "name": item.name or _file_name_from_source(source)})
            else:
                logger.warning("OneBot 文件发送已跳过：文件片段缺少 file/url")
        if action_index == 0 and message.text.strip():
            await send(message_action, {**target, "message": message.text})
        return None


def _flatten(segments: MessageChain) -> List[Segment]:
    result: List[Segment] = []
    for seg in segments:
        if isinstance(seg, ExtensionSegment):
            result.extend(_flatten(MessageChain(segment_registry.fallback(seg))))
        else:
            result.append(seg)
    return result


def _with_platform_reply_id(session_ctx: Any, seg: Reply) -> Reply:
    """Replies quote the platform id of the target message when it is known."""
    ref = seg.ref
    reply_id = ref.platform_message_id or (ref.message_id or "")
    platform_id = session_ctx.log.platform_id_of(reply_id) if reply_id else None
    if not platform_id:
        return seg
    seg = seg.model_copy(deep=True)
    seg.ref.platform_message_id = platform_id
    return seg


onebot_outbound_port = OneBotOutboundPort()


def _build_echo(session_id: str, message_id: str) -> str:
    payload = {"session_id": session_id, "message_id": str(message_id)}
    return json.dumps(payload, ensure_ascii=False)


def parse_onebot_echo(echo: Any) -> Tuple[Optional[str], Optional[str]]:
    if not echo:
        return None, None
    if isinstance(echo, (dict,)):
        return echo.get("session_id") or echo.get("sid"), echo.get("message_id") or echo.get("mid")
    if not isinstance(echo, str):
        return None, None
    try:
        data = json.loads(echo)
    except Exception:
        return None, None
    if isinstance(data, dict):
        return data.get("session_id") or data.get("sid"), data.get("message_id") or data.get("mid")
    return None, None
