from __future__ import annotations

from typing import Any, Callable, Dict, Optional
import re
import json
import asyncio
import time

from app.logger import setup_logger
from app.adapters.onebot_v11.config import onebot_v11_config
from app.conversation.session import ConversationSession
from app.inbound import ConversationRef, InboundEvent, PlatformFacts, Sender, SessionIdOptions
from app.inbound import pipeline
from app.adapters.onebot_v11.store.action_tracker import onebot_action_tracker
from app.adapters.onebot_v11.store.role_store import (
    ROLE_TTL as _BOT_GROUP_ROLE_TTL,
    VALID_GROUP_ROLES as _VALID_GROUP_ROLES,
    get_cached_bot_role,
    normalize_group_role,
    remember_bot_role,
    unknown_cooldown,
)


logger = setup_logger(__name__)

# 全局共享缓存：存储 (group_id, self_id) -> (role, timestamp)
# 将生命周期提到进程级别，以实现跨用户会话共享，避免每个群员发消息都重复请求外部 API
_BOT_GROUP_ROLE_CACHE: Dict[tuple[int, int], tuple[str, float]] = {}

# 全局群锁映射：存储 (group_id, self_id) -> asyncio.Lock
# 用于针对单群的 API 请求进行多协程并发防抖与排队保护，防止突发流量轰炸 API
_BOT_GROUP_ROLE_LOCKS: Dict[tuple[int, int], asyncio.Lock] = {}

# 接口查不到身份的记录：(group_id, self_id) -> (连续失败次数, 最近失败时间)，冷却随失败次数加长
_BOT_GROUP_ROLE_FAILURES: Dict[tuple[int, int], tuple[int, float]] = {}


def _cache_bot_group_role_from_event(event: Dict[str, Any]) -> Optional[str]:
    message_type = event.get("message_type")
    group_id = event.get("group_id")
    self_id = event.get("self_id") or event.get("user_id")
    sender_info = event.get("sender") or {}
    role = normalize_group_role(sender_info.get("role"))
    if message_type != "group" or not group_id or not self_id or not role:
        return None
    try:
        cache_key = (int(group_id), int(self_id))
    except (TypeError, ValueError):
        return None
    return remember_bot_role(_BOT_GROUP_ROLE_CACHE, cache_key[0], cache_key[1], role, source="message_sent")


def _is_self_sent_message(event: Dict[str, Any]) -> bool:
    return (
        (event.get("post_type") or "").lower() == "message_sent"
        or event.get("message_sent_type") == "self"
        or event.get("sub_type") == "self"
    )


def is_mentioned(event: Dict[str, Any], message: Any, self_id: Any) -> bool:
    if self_id is None:
        return False
    self_id_str = str(self_id)
    segments = event.get("message")
    if isinstance(segments, list):
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") not in ("at", "poke"):
                continue
            data = seg.get("data") or {}
            if str(data.get("qq")) == self_id_str:
                return True
    
    # 修复：使用正则确保精确匹配，避免 ID 前缀匹配问题
    if isinstance(message, str) and self_id_str:
        # 匹配 [CQ:at,qq=ID] 或 [CQ:poke,qq=ID]
        pattern = r"\[CQ:(?:at|poke),qq=" + re.escape(self_id_str) + r"(?:,|\])"
        return bool(re.search(pattern, message))
    return False


def _extract_mentioned_ids(*values: Any) -> list[str]:
    """Return platform mention targets without inferring conversational meaning."""
    found: list[str] = []

    def add(value: Any) -> None:
        candidate = str(value or "").strip()
        if candidate and candidate not in found:
            found.append(candidate)

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if isinstance(value, dict):
            if str(value.get("type") or "").lower() in {"at", "poke"}:
                data = value.get("data") or {}
                if isinstance(data, dict):
                    add(data.get("qq") or data.get("id") or data.get("user_id"))
            for key in ("message", "raw_message"):
                if key in value:
                    visit(value.get(key))
            return
        text = str(value or "")
        patterns = (
            r"\[(?:CQ:)?(?:at|poke)\b(?P<params>[^\]]*)\]",
            r"\[at\s*,\s*id\s*=\s*(?P<id>[^\],\s]+)",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                direct_id = match.groupdict().get("id")
                if direct_id:
                    add(direct_id)
                    continue
                params = _parse_segment_params((match.groupdict().get("params") or "").lstrip(","))
                add(params.get("qq") or params.get("id") or params.get("user_id"))

    for item in values:
        visit(item)
    return found


def _parse_segment_params(params_str: str) -> Dict[str, str]:
    params: Dict[str, str] = {}
    for part in str(params_str or "").split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        params[key.strip().lower()] = value.strip()
    return params


def _extract_reply_platform_id(value: Any) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, list):
        for seg in value:
            reply_id = _extract_reply_platform_id(seg)
            if reply_id:
                return reply_id
        return None

    if isinstance(value, dict):
        if str(value.get("type") or "").lower() == "reply":
            data = value.get("data") or {}
            if isinstance(data, dict):
                reply_id = data.get("id") or data.get("message_id")
                if reply_id is not None:
                    return str(reply_id).strip()
        for key in ("message", "raw_message"):
            reply_id = _extract_reply_platform_id(value.get(key))
            if reply_id:
                return reply_id
        return None

    text = str(value or "")
    for match in re.finditer(r"\[(?:CQ:)?reply\b(?P<params>[^\]]*)\]", text, flags=re.IGNORECASE):
        params = _parse_segment_params((match.group("params") or "").lstrip(","))
        reply_id = params.get("id") or params.get("platform_id") or params.get("platform_message_id")
        if reply_id:
            return str(reply_id).strip()
    return None


async def _fetch_and_cache_self_role(
    session_ctx: ConversationSession,
    message_type: str,
    group_id: Optional[int],
    self_id: Optional[int],
    sender: Any,
):
    """取 Bot 在群里的身份，写进 ``platform_state["self_role"]``。

    身份主要靠事件得知：Bot 自己发言的回显带 sender.role，被设/取消管理员有
    group_admin 通知；两者都会更新进程内缓存和持久化缓存。接口只在缓存过期
    （6 小时）时核对一次，查不到时沿用上次知道的身份；一直查不到就按 60s、
    120s……最长 30 分钟退避，只有第一次失败记警告。
    """
    if message_type != "group" or not group_id or not self_id:
        return

    try:
        g_id = int(group_id)
        s_id = int(self_id)
    except (ValueError, TypeError) as e:
        logger.error(f"解析 group_id ({group_id}) 或 self_id ({self_id}) 失败，安全退避为 member: {e}")
        session_ctx.platform_state["self_role"] = "member"
        return

    cache_key = (g_id, s_id)

    event_role = normalize_group_role(
        (session_ctx.platform_state.get("onebot_last_self_sent") or {}).get("sender_role")
    )
    if event_role:
        remember_bot_role(_BOT_GROUP_ROLE_CACHE, g_id, s_id, event_role, source="session_note")
        session_ctx.platform_state["self_role"] = event_role
        return

    def cooling_down() -> bool:
        failed = _BOT_GROUP_ROLE_FAILURES.get(cache_key)
        return bool(failed) and time.time() - failed[1] < unknown_cooldown(failed[0])

    def fallback() -> str:
        return get_cached_bot_role(_BOT_GROUP_ROLE_CACHE, g_id, s_id, allow_stale=True) or "member"

    cached_role = get_cached_bot_role(_BOT_GROUP_ROLE_CACHE, g_id, s_id, ttl=_BOT_GROUP_ROLE_TTL)
    if cached_role:
        session_ctx.platform_state["self_role"] = cached_role
        return
    if cooling_down():
        session_ctx.platform_state["self_role"] = fallback()
        return

    # 同一群的并发消息排队，同一时刻最多一次接口请求
    lock = _BOT_GROUP_ROLE_LOCKS.setdefault(cache_key, asyncio.Lock())
    async with lock:
        cached_role = get_cached_bot_role(_BOT_GROUP_ROLE_CACHE, g_id, s_id, ttl=_BOT_GROUP_ROLE_TTL)
        if cached_role:
            session_ctx.platform_state["self_role"] = cached_role
            return
        if cooling_down():
            session_ctx.platform_state["self_role"] = fallback()
            return

        error: Optional[Exception] = None
        role_val: Optional[str] = None
        try:
            from app.adapters.onebot_v11.tools.utils import fetch_group_member_role

            role_val = await fetch_group_member_role(
                g_id,
                s_id,
                onebot_action_tracker,
                sender,
                no_cache=False,
                timeout=2.0,
            )
        except Exception as e:
            error = e
        if role_val:
            _BOT_GROUP_ROLE_FAILURES.pop(cache_key, None)
            remember_bot_role(_BOT_GROUP_ROLE_CACHE, g_id, s_id, role_val, source="api")
            session_ctx.platform_state["self_role"] = role_val
            logger.info(f"已通过 OneBot API 获取并全局缓存 Bot({s_id}) 在群({g_id}) 的权限: {role_val}")
            return

        failures = _BOT_GROUP_ROLE_FAILURES.get(cache_key, (0, 0.0))[0] + 1
        _BOT_GROUP_ROLE_FAILURES[cache_key] = (failures, time.time())
        role = fallback()
        session_ctx.platform_state["self_role"] = role
        reason = f"发生异常: {error}" if error else "所有接口均无有效响应"
        message = (
            f"获取 Bot({s_id}) 自身群({g_id}) 权限{reason}，暂按 {role} 处理，"
            f"{unknown_cooldown(failures):.0f}s 后再试（第 {failures} 次）"
        )
        # 连续失败只在第一次提醒；之后的重试结果留在调试日志里
        if failures == 1:
            logger.warning(message)
        else:
            logger.debug(message)


def _remember_role_from_notice(event: Dict[str, Any]) -> Optional[str]:
    """group_admin 通知：Bot 自己被设为/取消管理员时直接更新身份缓存。"""
    if event.get("notice_type") != "group_admin":
        return None
    try:
        g_id, s_id, user_id = int(event.get("group_id")), int(event.get("self_id")), int(event.get("user_id"))
    except (TypeError, ValueError):
        return None
    if user_id != s_id:
        return None
    role = {"set": "admin", "unset": "member"}.get(str(event.get("sub_type") or ""))
    if not role:
        return None
    _BOT_GROUP_ROLE_FAILURES.pop((g_id, s_id), None)
    remember_bot_role(_BOT_GROUP_ROLE_CACHE, g_id, s_id, role, source="notice")
    logger.info(f"收到群管理变动通知，Bot({s_id}) 在群({g_id}) 的权限更新为: {role}")
    return role


def _log_onebot_non_message(event: Dict[str, Any]) -> None:
    post_type = (event.get("post_type") or "unknown").lower()
    sub_type = event.get("sub_type") or ""
    detail = event.get("notice_type") or event.get("request_type") or event.get("meta_event_type") or ""
    meta_event = event.get("meta_event_type") or ""
    cfg = onebot_v11_config.get_config()
    log_cfg = cfg.get("logging") or {}
    if post_type == "meta_event":
        if not log_cfg.get("log_meta", True):
            return
        if meta_event == "heartbeat" and not log_cfg.get("log_heartbeat", False):
            return
    if post_type == "notice" and not log_cfg.get("log_notice", True):
        return
    if post_type == "request" and not log_cfg.get("log_request", True):
        return
    summary = f"OneBot 收到通知: post_type={post_type}"
    if detail:
        summary += f", detail={detail}"
    if sub_type:
        summary += f", sub_type={sub_type}"
    logger.info(summary)
    if log_cfg.get("debug_full_event", True):
        logger.debug(f"OneBot 通知完整内容: {json.dumps(event, ensure_ascii=False)}")


_REPLY_PREVIEW_LIMIT = 120
_REPLY_FETCH_FAILED = "[消息获取失败]"


class OneBotBinding:
    """How the framework pipeline reaches back into OneBot for one event."""

    def __init__(
        self,
        event: Dict[str, Any],
        account_id: str,
        platform: str,
        log_cfg: Dict[str, Any],
    ) -> None:
        self.event = event
        self.account_id = account_id
        self.platform = platform
        self.log_cfg = log_cfg

    def on_conversation(self, session_ctx: ConversationSession) -> None:
        pass

    async def on_command(self, session_ctx: ConversationSession, result: Any) -> None:
        pass

    async def before_agent(self, session_ctx: ConversationSession) -> None:
        event = self.event
        await _fetch_and_cache_self_role(
            session_ctx, event.get("message_type"), event.get("group_id"), event.get("self_id"), session_ctx.sender
        )

    async def fetch_message(self, session_ctx: ConversationSession, platform_message_id: str) -> Any:
        sender = session_ctx.sender
        if not sender:
            return None
        params: Dict[str, Any] = (
            {"message_id": int(platform_message_id)} if platform_message_id.isdigit() else {"message_id": platform_message_id}
        )
        response = await onebot_action_tracker.request(sender, "get_msg", params, timeout=3.0)
        if not response or response.get("status") not in ("ok", "success"):
            return None
        data = response.get("data")
        if not isinstance(data, dict):
            return None
        return data.get("raw_message") or data.get("message")

    def log_message(self, segments: Any) -> None:
        if not self.log_cfg.get("log_message", True):
            return
        event = self.event
        message_type = event.get("message_type")
        group_id = event.get("group_id")
        if message_type == "group" and group_id is not None:
            source_info = f"[群:{group_id}]"
        elif message_type == "private":
            source_info = "[私聊]"
        else:
            source_info = f"[{message_type or '未知'}]"
        self_id = event.get("self_id")
        bot_info = f"[Bot:{self_id}]" if self_id else ""
        sender = _sender_from_event(event)
        logger.info(
            f"OneBot 收到消息 {source_info}{bot_info} {sender.name}({sender.id}): {segments.summary()}"
        )
        if self.log_cfg.get("debug_full_message", True):
            logger.debug(f"OneBot 收到消息(完整): {event.get('raw_message') or event.get('message')}")
        if self.log_cfg.get("debug_full_event", True):
            logger.debug(f"OneBot 事件完整内容: {json.dumps(event, ensure_ascii=False)}")

    def on_self_message(self, session_ctx: ConversationSession) -> None:
        event = self.event
        role = _cache_bot_group_role_from_event(event)
        if role:
            session_ctx.platform_state["self_role"] = role
            session_ctx.platform_state["onebot_last_self_sent"] = {
                "group_id": event.get("group_id"),
                "self_id": event.get("self_id"),
                "sender_role": role,
                "message_id": event.get("message_id"),
                "time": event.get("time"),
            }
            logger.info(f"已从 Bot 自身消息回显缓存 Bot({event.get('self_id')}) 在群({event.get('group_id')}) 的权限: {role}")
        if self.log_cfg.get("log_message", True):
            logger.info(
                "OneBot 收到 Bot 自身消息回显 [群:%s][Bot:%s][role:%s] message_id=%s: %s",
                event.get("group_id"),
                event.get("self_id"),
                role or (event.get("sender") or {}).get("role", "unknown"),
                event.get("message_id"),
                str(event.get("raw_message") or event.get("message") or "")[:200],
            )
        if self.log_cfg.get("debug_full_event", True):
            logger.debug(f"OneBot 自身消息回显完整事件: {json.dumps(event, ensure_ascii=False)}")


def _sender_from_event(event: Dict[str, Any]) -> Sender:
    sender_info = event.get("sender") or {}
    name = sender_info.get("nickname") or sender_info.get("card") or "OneBot User"
    role = sender_info.get("role")
    role_labels = {"owner": "群主", "admin": "管理员"}
    if role in role_labels:
        name = f"{name} ({role_labels[role]})"
    return Sender(id=str(event.get("user_id") or "onebot_user"), name=name, role=role)


def _session_options(cfg: Dict[str, Any]) -> SessionIdOptions:
    return SessionIdOptions(
        prefix=cfg.get("session_id_prefix", "onebot"),
        use_group_as_session=cfg.get("use_group_as_session", True),
        include_bot_id=cfg.get("session_id_include_bot_id", True),
    )


def _conversation(platform: str, event: Dict[str, Any], account_id: str, user_id: str) -> ConversationRef:
    group_id = event.get("group_id")
    is_group = event.get("message_type") == "group" and group_id is not None
    self_id = event.get("self_id")
    return ConversationRef(
        platform=platform,
        scope="group" if is_group else "private",
        id=str(group_id) if is_group else user_id,
        account_id=account_id,
        self_id=str(self_id) if self_id is not None else None,
    )


async def handle_onebot_event(event: Dict[str, Any], account_id: str = "default") -> None:
    """Decode one OneBot v11 event and hand it to the framework pipeline."""
    post_type = (event.get("post_type") or "message").lower()
    cfg = onebot_v11_config.get_account(account_id) or onebot_v11_config.get_config()
    log_cfg = cfg.get("logging") or {}
    platform = cfg.get("platform_name", "onebot")
    binding = OneBotBinding(event, account_id, platform, log_cfg)
    options = _session_options(cfg)

    if _is_self_sent_message(event):
        self_id = event.get("self_id") or event.get("user_id")
        user_id = str(event.get("user_id") or self_id or "onebot_bot")
        sender_info = event.get("sender") or {}
        await pipeline.submit(InboundEvent(
            kind="self_message",
            conversation=_conversation(platform, {**event, "self_id": self_id}, account_id, user_id),
            sender=Sender(id=user_id, name=sender_info.get("nickname") or sender_info.get("card") or "OneBot Bot"),
            raw_content=event.get("raw_message") or event.get("message") or "",
            raw_text=str(event.get("raw_message") or event.get("message") or ""),
            binding=binding,
            session_options=options,
            platform_message_id=str(event["message_id"]) if event.get("message_id") is not None else None,
            raw_event=event,
        ))
        return

    notice_type: Optional[str] = None
    if post_type == "notice" and event.get("sub_type") == "poke":
        # A poke is a notice; it reaches the conversation as a poke segment.
        notice_type = "poke"
        target_id = event.get("target_id")
        event = {
            **event,
            "message_type": "group" if event.get("group_id") else "private",
            "message": f"[CQ:poke,qq={target_id}]",
        }
        binding.event = event
        logger.info(f"OneBot 收到戳一戳: target={target_id}")
    elif post_type != "message":
        if post_type == "notice":
            _remember_role_from_notice(event)
        _log_onebot_non_message(event)
        return

    message = event.get("message")
    if not isinstance(message, list):
        message = event.get("raw_message") or message or ""
    self_id = event.get("self_id")
    sender = _sender_from_event(event)
    await pipeline.submit(InboundEvent(
        kind="notice" if notice_type else "message",
        notice_type=notice_type,
        conversation=_conversation(platform, event, account_id, sender.id),
        sender=sender,
        raw_content=message,
        raw_text=str(event.get("raw_message") or message or ""),
        binding=binding,
        session_options=options,
        platform_message_id=str(event["message_id"]) if event.get("message_id") is not None else None,
        facts=PlatformFacts(
            mentions_self=is_mentioned(event, message, self_id),
            mentioned_ids=tuple(_extract_mentioned_ids(event, message)),
            reply_to_platform_id=_extract_reply_platform_id(event) or _extract_reply_platform_id(message),
        ),
        raw_event=event,
    ))


def _event_key(event: Dict[str, Any], account_id: str) -> str:
    """Events of one conversation run in order; different conversations do not wait on each other."""
    group_id = event.get("group_id")
    if group_id:
        return f"{account_id}:group:{group_id}"
    user_id = event.get("user_id") or event.get("target_id")
    if user_id:
        return f"{account_id}:private:{user_id}"
    return f"{account_id}:other"


try:
    from app.inbound.dispatch import OrderedDispatcher

    _dispatcher: Optional[OrderedDispatcher] = OrderedDispatcher("OneBot")
except ImportError:  # 宿主低于 4.8.43：退回逐个处理
    _dispatcher = None


async def dispatch_onebot_event(event: Dict[str, Any], account_id: str = "default") -> None:
    """Called from the receive loop: hand the event off so the loop keeps reading.

    The replies to our own API calls (``echo``) arrive on the same socket. Awaiting
    the event here meant every call made while handling it (the bot's group role,
    a quoted message) could only time out, and a long command stalled the account.
    """
    if _dispatcher is None:
        await handle_onebot_event(event, account_id=account_id)
        return
    _dispatcher.submit(_event_key(event, account_id), lambda: handle_onebot_event(event, account_id=account_id))

