from __future__ import annotations

import uuid
from typing import Any, Dict, Optional, Tuple
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from starlette.websockets import WebSocket, WebSocketState

from app.logger import setup_logger
from app.utils.metrics_manager import metrics


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


class OneBotReplySenderBase:
    def __init__(self, session_ctx):
        self._session_ctx = session_ctx

    @property
    def client_state(self) -> WebSocketState:
        return WebSocketState.CONNECTED

    async def send_json(self, data: Dict[str, Any]):
        if not isinstance(data, dict):
            return

        reply = data.get("reply")
        if reply is None and data.get("type") == "assistant_message":
            reply = data.get("content") or (data.get("message") or {}).get("content")
        if reply is None:
            return
        reply = self._replace_reply_id(str(reply))
        message_id = data.get("message_id") or data.get("id") or (data.get("message") or {}).get("message_id")
        echo = _build_echo(self._session_ctx.session_id, message_id) if message_id else None

        target = self._session_ctx.session_notes.get("onebot_target", {})
        message_type = target.get("message_type")
        group_id = target.get("group_id")
        user_id = target.get("user_id")

        if message_type == "group" and group_id:
            await self._send_reply_parts(
                reply,
                message_action="send_group_msg",
                message_target={"group_id": _coerce_int(group_id)},
                upload_action="upload_group_file",
                upload_target={"group_id": _coerce_int(group_id)},
                echo=echo,
            )
            self._track_outbound_reply(reply)
            return

        if user_id:
            await self._send_reply_parts(
                reply,
                message_action="send_private_msg",
                message_target={"user_id": _coerce_int(user_id)},
                upload_action="upload_private_file",
                upload_target={"user_id": _coerce_int(user_id)},
                echo=echo,
            )
            self._track_outbound_reply(reply)
            return

        logger.warning("OneBot 回复丢弃：缺少目标信息")

    async def _send_reply_parts(
        self,
        reply: str,
        *,
        message_action: str,
        message_target: Dict[str, Any],
        upload_action: str,
        upload_target: Dict[str, Any],
        echo: Optional[str],
    ) -> None:
        """Send regular message content and file uploads through their proper APIs."""
        cursor = 0
        action_index = 0

        async def send(action: str, params: Dict[str, Any]) -> None:
            nonlocal action_index
            action_echo = echo if action_index == 0 else None
            action_index += 1
            await self._send_action(action, params, action_echo)

        for match in _CQ_FILE_PATTERN.finditer(reply):
            message_part = reply[cursor:match.start()]
            if message_part.strip():
                await send(message_action, {**message_target, "message": message_part})

            file_params = _parse_cq_params(match.group("params"))
            source = file_params.get("file") or file_params.get("url")
            if source:
                name = file_params.get("name") or _file_name_from_source(source)
                await send(upload_action, {**upload_target, "file": source, "name": name})
            else:
                logger.warning("OneBot 文件发送已跳过：CQ:file 缺少 file/url 参数")
            cursor = match.end()

        trailing = reply[cursor:]
        if trailing.strip():
            await send(message_action, {**message_target, "message": trailing})
        elif action_index == 0 and reply.strip():
            await send(message_action, {**message_target, "message": reply})

    async def _send_action(self, action: str, params: Dict[str, Any], echo: Optional[str] = None):
        raise NotImplementedError

    async def send_action(self, action: str, params: Dict[str, Any], echo: Optional[str] = None) -> bool:
        try:
            await self._send_action(action, params, echo=echo)
            return True
        except Exception as e:
            logger.warning(f"OneBot 动作发送失败: {action}, 错误: {e}")
            return False

    def _track_outbound_reply(self, reply: str) -> None:
        adapter_name = self._session_ctx.platform or "onebot"
        metrics.track_adapter_message(
            adapter_name,
            "out",
            self._session_ctx.session_id,
            reply[:1000] if isinstance(reply, str) else str(reply)[:1000],
        )

    def _replace_reply_id(self, text: str) -> str:
        import re
        pattern = r"\[CQ:reply,id=([^\],]+)"
        def repl(match: re.Match) -> str:
            message_id = match.group(1)
            platform_id = self._session_ctx.resolve_platform_id(message_id)
            if platform_id:
                return f"[CQ:reply,id={platform_id}"
            return match.group(0)
        return re.sub(pattern, repl, text)


class ForwardOneBotReplySender(OneBotReplySenderBase):
    def __init__(self, client, session_ctx):
        super().__init__(session_ctx)
        self._client = client

    @property
    def client_state(self) -> WebSocketState:
        return WebSocketState.CONNECTED

    async def _send_action(self, action: str, params: Dict[str, Any], echo: Optional[str] = None):
        await self._client.send_action(action, params, echo=echo)


class ReverseOneBotReplySender(OneBotReplySenderBase):
    def __init__(self, websocket: WebSocket, session_ctx):
        super().__init__(session_ctx)
        self._websocket = websocket

    @property
    def client_state(self) -> WebSocketState:
        return self._websocket.client_state

    async def _send_action(self, action: str, params: Dict[str, Any], echo: Optional[str] = None):
        if self._websocket.client_state != WebSocketState.CONNECTED:
            return
        payload = {"action": action, "params": params, "echo": echo or str(uuid.uuid4())}
        await self._websocket.send_json(payload)


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
