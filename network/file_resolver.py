"""OneBot (NapCat) file capability: trade a file segment for a signed download URL.

The ftn URL inside group-file messages rejects direct downloads (HTTP 405);
``get_group_file_url`` / ``get_private_file_url`` return one that works.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.media.files import FileUnavailable, ResolvedFile
from app.message import File


class _AccountSender:
    """Adapts the outbound port to the action tracker's ``send_action(action, params, echo)``."""

    def __init__(self, port: Any, account_id: str):
        self._port = port
        self._account_id = account_id

    async def send_action(self, action: str, params: Dict[str, Any], echo: Optional[str] = None) -> bool:
        return await self._port.send_action(self._account_id, action, params, echo=echo)


def _int_or_str(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


class OneBotFileResolver:
    async def resolve_file(self, session: Any, segment: File) -> ResolvedFile:
        from app.adapters.onebot_v11.network.senders import onebot_outbound_port
        from app.adapters.onebot_v11.store.action_tracker import onebot_action_tracker

        conversation = getattr(session, "conversation", None)
        if conversation is None:
            raise FileUnavailable("缺少会话路由信息，无法向平台请求文件")
        # 新解析的文件段 media.file 是 file_id；旧历史里是文件名，NapCat 同样接受。
        file_id = segment.media.file or segment.name
        if not file_id:
            raise FileUnavailable("文件消息缺少 file_id")
        if conversation.is_group:
            action, params = "get_group_file_url", {"group_id": _int_or_str(conversation.id), "file_id": file_id}
        else:
            action, params = "get_private_file_url", {"file_id": file_id}

        sender = _AccountSender(onebot_outbound_port, conversation.account_id)
        response = await onebot_action_tracker.request(sender, action, params, timeout=10.0)
        data = (response or {}).get("data") or {}
        url = data.get("url") if isinstance(data, dict) else None
        if not response or response.get("status") not in ("ok", "success") or not url:
            message = (response or {}).get("message") or (response or {}).get("wording") or "平台未返回下载地址"
            raise FileUnavailable(f"{message}（文件可能已过期或被删除，或账号未连接）")
        return ResolvedFile(url=str(url), name=segment.name or "", size=segment.size)


onebot_file_resolver = OneBotFileResolver()
