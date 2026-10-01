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
    UNKNOWN_COOLDOWN as _BOT_GROUP_ROLE_COOLDOWN,
    VALID_GROUP_ROLES as _VALID_GROUP_ROLES,
    get_cached_bot_role,
    normalize_group_role,
    remember_bot_role,
)


logger = setup_logger(__name__)

# 全局共享缓存：存储 (group_id, self_id) -> (role, timestamp)
# 将生命周期提到进程级别，以实现跨用户会话共享，避免每个群员发消息都重复请求外部 API
_BOT_GROUP_ROLE_CACHE: Dict[tuple[int, int], tuple[str, float]] = {}

# 全局群锁映射：存储 (group_id, self_id) -> asyncio.Lock
# 用于针对单群的 API 请求进行多协程并发防抖与排队保护，防止突发流量轰炸 API
_BOT_GROUP_ROLE_LOCKS: Dict[tuple[int, int], asyncio.Lock] = {}

# 正常的缓存有效期为 10 分钟 (600秒)
_BOT_GROUP_ROLE_TTL = 600.0


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
    """
    异步获取并缓存 Bot 自身的群身份。
    
    【核心设计考量 - 首席架构师保障方案】：
    1. 全局共享：改用全局进程级 `_BOT_GROUP_ROLE_CACHE`，让该群内的所有用户发消息时能够瞬时共享同一个 Bot 角色缓存。
    2. 并发防抖 (Double-Checked Locking)：对于瞬间爆发的多协程群消息，利用各群专属的 `asyncio.Lock` 进行排队限制。
       协程在获得锁后，二次检查缓存状态，确保对于并发流量，同一秒内最多只会有 1 次物理网络请求到达 OneBot。
    3. 失败冷却退避 (Fallback & Cooldown)：若 OneBot 偶尔超时或限频报错，写入 "unknown" 标识，并在接下来的 60s 内
       拒绝一切针对该群的网络 API 获取请求。此冷却期内的请求会直接优雅降级为安全 fallback 值 "member"，
       彻底断绝“请求失败 -> 每次消息都再次强制重试 -> 永久陷入超频被封禁”的恶性网络雪崩。
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
    now = time.time()

    event_role = normalize_group_role(
        (session_ctx.platform_state.get("onebot_last_self_sent") or {}).get("sender_role")
    )
    if event_role:
        remember_bot_role(_BOT_GROUP_ROLE_CACHE, g_id, s_id, event_role, source="session_note")
        session_ctx.platform_state["self_role"] = event_role
        return

    # 1. 尝试首轮从全局缓存快速读取 (Lock-Free Fast Path)
    cached_role = get_cached_bot_role(_BOT_GROUP_ROLE_CACHE, g_id, s_id, ttl=_BOT_GROUP_ROLE_TTL)
    if cached_role:
        session_ctx.platform_state["self_role"] = cached_role
        return
    if cache_key in _BOT_GROUP_ROLE_CACHE:
        role, ts = _BOT_GROUP_ROLE_CACHE[cache_key]
        if role == "unknown" and (now - ts < _BOT_GROUP_ROLE_COOLDOWN):
            # 处于失败退避冷却期内，直接安全降级为 member，不发任何网络请求
            session_ctx.platform_state["self_role"] = "member"
            return

    # 2. 动态初始化该群专属的并发协程排队锁
    if cache_key not in _BOT_GROUP_ROLE_LOCKS:
        _BOT_GROUP_ROLE_LOCKS[cache_key] = asyncio.Lock()
    lock = _BOT_GROUP_ROLE_LOCKS[cache_key]

    # 3. 申请锁进入临界保护区，开始防抖处理
    async with lock:
        now = time.time()
        # 4. 二次检查缓存 (Double-Checked Locking)
        cached_role = get_cached_bot_role(_BOT_GROUP_ROLE_CACHE, g_id, s_id, ttl=_BOT_GROUP_ROLE_TTL)
        if cached_role:
            session_ctx.platform_state["self_role"] = cached_role
            return
        if cache_key in _BOT_GROUP_ROLE_CACHE:
            role, ts = _BOT_GROUP_ROLE_CACHE[cache_key]
            if role == "unknown" and (now - ts < _BOT_GROUP_ROLE_COOLDOWN):
                session_ctx.platform_state["self_role"] = "member"
                return

        # 5. 执行物理网络 API 调用，外层包装 Try-Except 及严格 Timeout，确保极致健壮性
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
            if role_val:
                remember_bot_role(_BOT_GROUP_ROLE_CACHE, g_id, s_id, role_val, source="api")
                session_ctx.platform_state["self_role"] = role_val
                logger.info(f"已通过 OneBot API 获取并全局缓存 Bot({s_id}) 在群({g_id}) 的权限: {role_val}")
            else:
                # 记录失败状态，触发 Cooldown，并安全 fallback 到 member
                _BOT_GROUP_ROLE_CACHE[cache_key] = ("unknown", time.time())
                session_ctx.platform_state["self_role"] = "member"
                logger.warning(f"获取 Bot({s_id}) 自身群({g_id}) 权限所有接口均无有效响应，已进入 60s 重试冷却退避。")
        except Exception as e:
            # 无论网络抖动、超时或 OneBot 崩塌，统一在此处捕获，写入 unknown 冷却状态，fallback 保障系统平稳运行
            _BOT_GROUP_ROLE_CACHE[cache_key] = ("unknown", time.time())
            session_ctx.platform_state["self_role"] = "member"
            logger.error(f"获取 Bot({s_id}) 自身群({g_id}) 权限发生异常: {e}，已进入 60s 重试冷却退避。")


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
        logger.info(f"OneBot 收到消息 {source_info}{bot_info} {sender.name}({sender.id}): {segments.summary()}")
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
