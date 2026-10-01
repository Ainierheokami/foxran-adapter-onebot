from __future__ import annotations

import uuid
from typing import Any, Dict, Optional, Tuple
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from starlette.websockets import WebSocketState

from app.logger import setup_logger


logger = setup_logger(__name__)


_CQ_FILE_PATTERN = re.compile(r"\[CQ:file,(?P<params>[^\]]+)\]")


def _coerce_int(value: Any) -> Any:
    try:
        return int(value)
    except Exception:
        return value


def _parse_cq_params(params_str: str) -> Dict[str, str]:
    params: Dict[str, str] = {}
    for part in params_str.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        params[key.strip()] = value.strip()
    return params


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

    def prepare(self, session_ctx: Any, reply: str) -> str:
        """Replace internal reply ids with platform message ids."""
        def repl(match: re.Match) -> str:
            platform_id = session_ctx.log.platform_id_of(match.group(1))
            return f"[CQ:reply,id={platform_id}" if platform_id else match.group(0)

        return re.sub(r"\[CQ:reply,id=([^\],]+)", repl, reply)

    async def deliver(self, session_ctx: Any, conversation: Any, reply: str, message_id: Optional[str]) -> None:
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

        # Regular content and file uploads go through their own APIs.
        cursor = 0
        for match in _CQ_FILE_PATTERN.finditer(reply):
            message_part = reply[cursor:match.start()]
            if message_part.strip():
                await send(message_action, {**target, "message": message_part})
            file_params = _parse_cq_params(match.group("params"))
            source = file_params.get("file") or file_params.get("url")
            if source:
                name = file_params.get("name") or _file_name_from_source(source)
                await send(upload_action, {**target, "file": source, "name": name})
            else:
                logger.warning("OneBot 文件发送已跳过：CQ:file 缺少 file/url 参数")
            cursor = match.end()

        trailing = reply[cursor:]
        if trailing.strip():
            await send(message_action, {**target, "message": trailing})
        elif action_index == 0 and reply.strip():
            await send(message_action, {**target, "message": reply})


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
