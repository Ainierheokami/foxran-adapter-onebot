from typing import Dict, Any, Optional
from app.tools.base import BaseTool, ToolType, ToolResult
from app.logger import setup_logger

logger = setup_logger(__name__)


_RESOURCE_MARKERS = (
    "[image",
    "[video",
    "[file",
    "[audio",
    "[meme",
    "[cq:image",
    "[cq:video",
    "[cq:file",
    "[cq:audio",
)


def _normalize_requested_message_id(value: Any) -> str:
    """Accept the plain ID as well as labels copied from model history."""
    message_id = str(value or "").strip()
    for prefix in ("platform_message_id=", "internal_message_id=", "message_id="):
        if message_id.lower().startswith(prefix):
            message_id = message_id[len(prefix):].strip().strip("\"'")
            break
    return message_id


def _is_same_user(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return str(left).strip() == str(right).strip()


def _message_is_assistant(message: Any) -> bool:
    return (
        getattr(message, "role", None) in ("assistant", "bot")
        and not (getattr(message, "metadata", None) or {}).get("hidden")
    )


def _message_has_resource(message: Any) -> bool:
    content = str(getattr(message, "content", "") or "").lower()
    return any(marker in content for marker in _RESOURCE_MARKERS)


def _find_local_message(session_ctx: Any, requested_id: str) -> Optional[Any]:
    """Find a history message by internal ID, platform ID, or delivery ID."""
    get_by_internal = getattr(session_ctx, "get_message_by_internal_id", None)
    if callable(get_by_internal):
        message = get_by_internal(requested_id)
        if message is not None:
            return message

    resolve_internal = getattr(session_ctx, "resolve_internal_id_from_platform", None)
    get_by_id = getattr(session_ctx, "get_message_by_id", None)
    if callable(resolve_internal) and callable(get_by_id):
        internal_id = resolve_internal(requested_id)
        if internal_id:
            message = get_by_id(str(internal_id))
            if message is not None:
                return message

    for message in reversed(getattr(session_ctx, "history", []) or []):
        if str(getattr(message, "platform_message_id", "") or "") == requested_id:
            return message
        delivery_id = str((getattr(message, "metadata", None) or {}).get("delivery_id") or "")
        if delivery_id and requested_id == f"send:{delivery_id}":
            return message
    return None


def _find_recent_own_message(session_ctx: Any) -> Optional[Any]:
    """Find the latest own resource before the current user turn.

    This is only used when an internal send reference is stale or was guessed
    by the model. Prefer a resource so an acknowledgement such as "我来撤回"
    is not selected instead of the image/file the user asked to remove.
    """
    history = list(getattr(session_ctx, "history", []) or [])
    if not history:
        return None

    latest_user_index = next(
        (
            index
            for index in range(len(history) - 1, -1, -1)
            if getattr(history[index], "role", None) == "user"
            and not (getattr(history[index], "metadata", None) or {}).get("hidden")
        ),
        None,
    )
    candidates = history[:latest_user_index] if latest_user_index is not None else history
    assistant_messages = [
        message
        for message in candidates
        if _message_is_assistant(message)
        and str(getattr(message, "platform_message_id", "") or "").strip()
    ]
    if not assistant_messages:
        return None
    resource_messages = [message for message in assistant_messages if _message_has_resource(message)]
    return (resource_messages or assistant_messages)[-1]


def _resolve_target_message(session_ctx: Any, requested_id: Any) -> tuple[str, bool, Optional[Any]]:
    """Return (platform_id, is_known_own_message, local_message)."""
    requested = _normalize_requested_message_id(requested_id)
    local_message = _find_local_message(session_ctx, requested)

    if local_message is not None:
        platform_id = str(getattr(local_message, "platform_message_id", "") or "").strip()
        if not platform_id:
            resolve_platform = getattr(session_ctx, "resolve_platform_id", None)
            if callable(resolve_platform):
                platform_id = str(resolve_platform(requested) or "").strip()
        if platform_id:
            return platform_id, _message_is_assistant(local_message), local_message

    # An internal send ID can be generated before the adapter echo is bound.
    # Only fall back for the explicit ``send:...`` form; never guess a target
    # for an arbitrary malformed/nonnumeric ID.
    if requested.lower().startswith("send:"):
        fallback = _find_recent_own_message(session_ctx)
        if fallback is not None:
            platform_id = str(getattr(fallback, "platform_message_id", "") or "").strip()
            if platform_id:
                logger.warning(
                    "撤回请求中的消息 ID %s 未命中历史，已回退到最近一条 Bot 资源消息 %s",
                    requested,
                    platform_id,
                )
                return platform_id, True, fallback

    return requested, _message_is_assistant(local_message), local_message


class DeleteMsgTool(BaseTool):
    output_kind = "data"
    result_visibility = "user_visible"
    name: str = "delete_msg"
    description: str = (
        "撤回一条已发出的消息。优先使用历史中该消息的纯数字 platform_message_id；"
        "也可提供 assistant 消息的 internal_message_id，运行时会尝试映射。"
        "撤回 Bot 自己发出的消息不需要管理员权限；撤回其他成员消息才需要比较群权限。"
    )
    tool_type: ToolType = "direct"
    
    def get_input_schema_for_llm(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": (
                        "要撤回的消息ID。优先填历史中该 assistant 消息的纯数字 "
                        "platform_message_id；若只有 internal_message_id 也可以填写。"
                    )
                }
            },
            "required": ["message_id"]
        }
        
    async def execute(self, message_id: str = "", session_ctx: Optional[Any] = None, **kwargs) -> ToolResult:
        if not session_ctx:
            return ToolResult(self.name, False, error="缺少会话上下文，无法执行操作。")
            
        conversation = getattr(session_ctx, "conversation", None)
        session_notes = getattr(session_ctx, "session_notes", {}) or {}
        target_info = session_notes.get("onebot_target", {}) or {}
        group_id = target_info.get("group_id") or (
            conversation.group_id if conversation else None
        )
        self_id = target_info.get("self_id") or (
            session_notes.get("onebot_last_self_sent", {}) or {}
        ).get("self_id") or (conversation.self_id if conversation else None)
        
        sender = getattr(session_ctx, "websocket", None)
        if not sender:
            return ToolResult(self.name, False, error="找不到当前活跃的通讯发射器(sender)，可能连接已断开，无法操作。")
            
        try:
            from app.adapters.onebot_v11.store.action_tracker import onebot_action_tracker
            
            message_id, local_own_message, _local_message = _resolve_target_message(
                session_ctx,
                message_id,
            )
            # Agent 使用的是内部 short GUID，尝试将其映射回平台真实的数字 message_id。
            platform_id = session_ctx.log.platform_id_of(message_id)
            if platform_id:
                message_id = platform_id
            try:
                target_msg_id = int(message_id)
            except (TypeError, ValueError):
                return ToolResult(
                    self.name,
                    False,
                    error=f"无效的消息ID格式: {message_id}。请提供有效的纯数字 message_id 或正确的上下文内部ID。",
                )
            
            # 首先去获取这条消息的信息，确定发件人身份
            msg_info = await onebot_action_tracker.request(
                sender, 
                "get_msg", 
                {"message_id": target_msg_id}, 
                timeout=3.0
            )
            
            target_user_id = None
            msg_group_id = group_id
            
            if msg_info and msg_info.get("status") in ("ok", "success"):
                data = msg_info.get("data") or {}
                target_user_id = (data.get("sender") or {}).get("user_id")
                # 如果这个消息原本就不在群里，或者拿到了真实的 group_id，就使用真实的
                real_group_id = data.get("group_id")
                if real_group_id:
                    msg_group_id = real_group_id
            
            # 只有撤回别人的消息才需要权限比较。若本地历史已确认这是
            # assistant 自己发出的消息，不能被 get_msg 的字段差异误判。
            remote_own_message = _is_same_user(target_user_id, self_id)
            if msg_group_id and target_user_id and not (local_own_message or remote_own_message):
                if self_id is None:
                    return ToolResult(self.name, False, error="无法确认 Bot 身份，暂不能安全判断该消息是否属于自己。")
                from app.adapters.onebot_v11.tools.utils import verify_punish_permission
                permitted, reason = await verify_punish_permission(
                    int(msg_group_id), int(target_user_id), int(self_id), 
                    onebot_action_tracker, sender,
                    bot_role_hint=session_ctx.platform_state.get("self_role")
                )
                if not permitted:
                    return ToolResult(self.name, False, error=f"撤回失败（权限不足以撤回此成员的消息）：{reason}")
                    
            response = await onebot_action_tracker.request(
                sender, 
                "delete_msg", 
                {"message_id": target_msg_id}, 
                timeout=5.0
            )
            
            if response and response.get("status") in ("ok", "success"):
                return ToolResult(self.name, True, f"撤回消息成功 (消息ID: {message_id})")
            else:
                return ToolResult(self.name, False, error=f"撤回消息失败: {response.get('msg') if response else '未知错误'}")
                
        except Exception as e:
            logger.error(f"撤回动作发生异常: {e}")
            return ToolResult(self.name, False, error=f"执行撤回异常: {e}")
