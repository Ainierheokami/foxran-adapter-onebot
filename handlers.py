from __future__ import annotations

from typing import Any, Callable, Dict, Optional
import re
import json
import asyncio
import time

from app.logger import setup_logger
from app.adapters.onebot_v11.config import onebot_v11_config
from app.api.core import get_or_create_session_context, active_processors
from app.adapters.control.stop_commands import is_stop_command
import app.api.core as api_core
from app.tasks.core.session_processor import SessionProcessor
from app.context.context_manager import SessionContext
from app.data_mappers import get_message_processor
from app.adapters.control.policy import platform_policy
from app.adapters.message_protocol import (
    bind_platform_id,
    make_platform_context_message,
    make_user_message,
    resolve_message_id_from_platform,
)
from app.adapters.onebot_v11.store.action_tracker import onebot_action_tracker
from app.adapters.onebot_v11.store.role_store import (
    UNKNOWN_COOLDOWN as _BOT_GROUP_ROLE_COOLDOWN,
    VALID_GROUP_ROLES as _VALID_GROUP_ROLES,
    get_cached_bot_role,
    normalize_group_role,
    remember_bot_role,
)
from app.utils.metrics_manager import metrics


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


def build_session_id(
    message_type: Optional[str],
    user_id: str,
    group_id: Optional[int],
    self_id: Optional[str],
) -> str:
    cfg = onebot_v11_config.get_config()
    prefix = cfg.get("session_id_prefix", "onebot")
    use_group = cfg.get("use_group_as_session", True)
    include_bot = cfg.get("session_id_include_bot_id", True)
    bot_part = f"{self_id}:" if include_bot and self_id else ""
    if use_group and message_type == "group" and group_id:
        return f"{prefix}:{bot_part}group:{group_id}"
    return f"{prefix}:{bot_part}user:{user_id}"


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


def _summarize_reply_target(target_msg: Any, limit: int = 120) -> str:
    if not target_msg or not getattr(target_msg, "content", None):
        return ""
    text = re.sub(r"\s+", " ", str(target_msg.content)).strip()
    text = text.replace("]", ")").replace(",", "，")
    return text[:limit] + "..." if len(text) > limit else text


def _enrich_reply_parts(parts: Any, session_ctx: SessionContext) -> list[Dict[str, Any]]:
    if not isinstance(parts, list):
        return parts or []

    enriched: list[Dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            enriched.append(part)
            continue

        next_part = dict(part)
        if str(next_part.get("type") or "").lower() == "reply":
            platform_id = (
                next_part.get("platform_id")
                or next_part.get("platform_message_id")
            )
            internal_id = (
                next_part.get("message_id")
                or next_part.get("internal_id")
                or next_part.get("reply_to_message_id")
            )

            bare_id = next_part.get("id")
            if bare_id is not None and not platform_id and not internal_id:
                platform_id = bare_id

            if platform_id is not None and not internal_id:
                internal_id = session_ctx.resolve_message_id_from_platform(platform_id)
            if internal_id and platform_id is None:
                platform_id = session_ctx.resolve_platform_message_id(str(internal_id))

            target_msg = session_ctx.get_message_by_id(str(internal_id)) if internal_id else None
            if internal_id:
                next_part["id"] = str(internal_id)
                next_part["message_id"] = str(internal_id)
                next_part["internal_id"] = str(internal_id)
                next_part["reply_to_message_id"] = str(internal_id)
            if platform_id is not None:
                next_part["platform_id"] = str(platform_id)
                next_part["platform_message_id"] = str(platform_id)
            if not str(next_part.get("text") or "").strip():
                snippet = _summarize_reply_target(target_msg)
                if snippet:
                    next_part["text"] = snippet

        enriched.append(next_part)
    return enriched


async def _fetch_and_cache_self_role(
    session_ctx: SessionContext,
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
        session_ctx.session_notes["self_role"] = "member"
        return

    cache_key = (g_id, s_id)
    now = time.time()

    event_role = normalize_group_role(
        (session_ctx.session_notes.get("onebot_last_self_sent") or {}).get("sender_role")
    )
    if event_role:
        remember_bot_role(_BOT_GROUP_ROLE_CACHE, g_id, s_id, event_role, source="session_note")
        session_ctx.session_notes["self_role"] = event_role
        return

    # 1. 尝试首轮从全局缓存快速读取 (Lock-Free Fast Path)
    cached_role = get_cached_bot_role(_BOT_GROUP_ROLE_CACHE, g_id, s_id, ttl=_BOT_GROUP_ROLE_TTL)
    if cached_role:
        session_ctx.session_notes["self_role"] = cached_role
        return
    if cache_key in _BOT_GROUP_ROLE_CACHE:
        role, ts = _BOT_GROUP_ROLE_CACHE[cache_key]
        if role == "unknown" and (now - ts < _BOT_GROUP_ROLE_COOLDOWN):
            # 处于失败退避冷却期内，直接安全降级为 member，不发任何网络请求
            session_ctx.session_notes["self_role"] = "member"
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
            session_ctx.session_notes["self_role"] = cached_role
            return
        if cache_key in _BOT_GROUP_ROLE_CACHE:
            role, ts = _BOT_GROUP_ROLE_CACHE[cache_key]
            if role == "unknown" and (now - ts < _BOT_GROUP_ROLE_COOLDOWN):
                session_ctx.session_notes["self_role"] = "member"
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
                session_ctx.session_notes["self_role"] = role_val
                logger.info(f"已通过 OneBot API 获取并全局缓存 Bot({s_id}) 在群({g_id}) 的权限: {role_val}")
            else:
                # 记录失败状态，触发 Cooldown，并安全 fallback 到 member
                _BOT_GROUP_ROLE_CACHE[cache_key] = ("unknown", time.time())
                session_ctx.session_notes["self_role"] = "member"
                logger.warning(f"获取 Bot({s_id}) 自身群({g_id}) 权限所有接口均无有效响应，已进入 60s 重试冷却退避。")
        except Exception as e:
            # 无论网络抖动、超时或 OneBot 崩塌，统一在此处捕获，写入 unknown 冷却状态，fallback 保障系统平稳运行
            _BOT_GROUP_ROLE_CACHE[cache_key] = ("unknown", time.time())
            session_ctx.session_notes["self_role"] = "member"
            logger.error(f"获取 Bot({s_id}) 自身群({g_id}) 权限发生异常: {e}，已进入 60s 重试冷却退避。")

async def handle_onebot_event(
    event: Dict[str, Any],
    sender_factory: Callable[[SessionContext], Any],
) -> None:
    post_type = (event.get("post_type") or "message").lower()
    cfg = onebot_v11_config.get_config()
    log_cfg = cfg.get("logging") or {}
    platform = cfg.get("platform_name", "onebot")
    
    # 拦截戳一戳事件，将其伪装成消息事件传递给核心处理
    if post_type == "notice" and event.get("sub_type") == "poke":
        post_type = "message"
        event["post_type"] = "message"
        event["message_type"] = "group" if event.get("group_id") else "private"
        target_id = event.get("target_id")
        event["message"] = f"[CQ:poke,qq={target_id}]"
        logger.info(f"OneBot 拦截到戳一戳事件，转换为消息处理: target={target_id}")

    if _is_self_sent_message(event):
        await _handle_onebot_self_sent_event(event, sender_factory, platform, log_cfg)
        return

    if post_type != "message":
        _log_onebot_non_message(event)
        return

    if not api_core.core_agent:
        logger.warning("核心代理未就绪：忽略 OneBot 事件")
        return

    message = event.get("message")
    if not isinstance(message, list):
        message = event.get("raw_message") or message or ""

    message_type = event.get("message_type")
    group_id = event.get("group_id")
    self_id = event.get("self_id")
    user_id = str(event.get("user_id") or "onebot_user")
    sender_info = event.get("sender") or {}
    user_name = sender_info.get("nickname") or sender_info.get("card") or "OneBot User"
    sender_role = sender_info.get("role")
    if sender_role in ("owner", "admin"):
        role_map = {"owner": "群主", "admin": "管理员"}
        user_name = f"{user_name} ({role_map[sender_role]})"

    # Track metrics for incoming message
    session_id_temp = build_session_id(
        message_type=message_type,
        user_id=user_id,
        group_id=group_id,
        self_id=str(self_id) if self_id is not None else None,
    )
    raw_message_text = str(event.get("raw_message") or message or "")
    if len(raw_message_text) > 1000:
        raw_message_text = raw_message_text[:1000] + "..."
    metrics.track_adapter_message(platform, "in", session_id_temp, raw_message_text)

    message_processor = get_message_processor()
    processed = None
    try:
        processed = await message_processor.process_incoming_message(
            platform=platform,
            raw_content=message,
            message_data={"role": "user", "content": message, "user_id": user_id, "user_name": user_name},
        )
    except Exception as e:
        logger.error(f"OneBot 入站处理失败: {e}")
        return

    _log_onebot_message(
        platform=platform,
        message_type=message_type,
        user_id=user_id,
        user_name=user_name,
        group_id=group_id,
        self_id=self_id,
        processed=processed,
        event=event,
        log_cfg=log_cfg,
    )

    session_id = build_session_id(
        message_type=message_type,
        user_id=user_id,
        group_id=group_id,
        self_id=str(self_id) if self_id is not None else None,
    )
    
    try:
        session_ctx = await get_or_create_session_context(
            session_id=session_id,
            user_id=user_id,
            user_name=user_name,
            platform=platform,
            bot_name=None,
            history_dicts=None,
        )
    except Exception as e:
        logger.error(f"创建会话上下文失败: {e}")
        return

    is_at = is_mentioned(event, message, self_id)
    
    # [新增] 检查是否回复了 Bot 之前的消息
    # 如果用户没有直接 @Bot，但是回复（引用）了 Bot 之前发出的消息，同样将其视为被 @（触发交互）
    if not is_at:
        reply_id_platform = _extract_reply_platform_id(event) or _extract_reply_platform_id(message)
        if reply_id_platform:
            message_id = resolve_message_id_from_platform(session_ctx, reply_id_platform)
            if message_id:
                msg_obj = session_ctx.get_message_by_id(message_id)
                # 兼容 role 可能的值：assistant 或 bot
                if msg_obj and msg_obj.role in ("assistant", "bot"):
                    is_at = True
                    logger.info(f"检测到用户引用了 Bot 的历史消息({reply_id_platform})，触发自动回复响应。")

    command = _parse_group_command(processed.internal)
    if command and message_type == "group":

        session_ctx.session_notes["onebot_target"] = {
            "message_type": message_type,
            "user_id": user_id,
            "group_id": group_id,
            "self_id": self_id,
            "sender_role": sender_info.get("role", "member")
        }
        session_ctx.set_websocket(sender_factory(session_ctx))

        if not is_at:
            await session_ctx.send_assistant_message("请 @我 后再使用群管理命令")
            return
        if not platform_policy.is_admin(
            platform=platform,
            bot_id=str(self_id) if self_id is not None else None,
            user_id=user_id,
            message_type=message_type,
            group_id=group_id,
        ):
            await session_ctx.send_assistant_message("权限不足：仅管理员可执行该命令")
            return

        handled, reply = await _handle_group_command(
            command=command,
            platform=platform,
            bot_id=str(self_id) if self_id is not None else None,
            group_id=group_id,
        )
        if handled and reply:
            await session_ctx.send_assistant_message(reply)
        return

    decision = platform_policy.evaluate(
        platform=platform,
        message_type=message_type,
        user_id=user_id,
        group_id=group_id,
        is_mention=is_at,
        bot_id=str(self_id) if self_id is not None else None,
    )
    if not decision.allowed:
        logger.debug(f"OneBot 消息被白名单策略拦截: reason={decision.reason}")
        return
    
    # 临时调试日志，用于排查艾特问题
    if decision.should_reply:
         logger.info(f"决定回复消息: reason={decision.reason}, is_at={is_at}, self_id={self_id}")

    session_ctx.session_notes["onebot_target"] = {
        "message_type": message_type,
        "user_id": user_id,
        "group_id": group_id,
        "self_id": self_id,
        "sender_role": sender_info.get("role", "member")
    }
    session_ctx.set_websocket(sender_factory(session_ctx))

    if is_stop_command(processed.internal):
        is_private = (message_type or "").lower() == "private"
        can_stop = is_private or is_at or platform_policy.is_admin(
            platform=platform,
            bot_id=str(self_id) if self_id is not None else None,
            user_id=user_id,
            message_type=message_type,
            group_id=group_id,
        )
        if not can_stop:
            logger.info("OneBot 停止命令被忽略：群聊中未 @ 且非管理员。")
            return
        processor = active_processors.get(session_ctx.session_id)
        if processor:
            await processor.abort()
            await session_ctx.send_assistant_message("已停止当前任务。")
            logger.info("OneBot 会话任务已被用户停止: %s", session_ctx.session_id)
        else:
            await session_ctx.send_assistant_message("当前没有正在执行的任务。")
        return

    if not decision.should_reply:
        platform_id = event.get("message_id")
        enriched_content = await _expand_reply_reference(processed.compressed, session_ctx)
        current_message = make_user_message(
            content=enriched_content,
            user_id=user_id,
            user_name=user_name,
            platform=platform,
            platform_id=platform_id,
            raw_content=processed.original,
            metadata={
                "message_parts": _enrich_reply_parts(processed.parts, session_ctx),
            },
        )
        bind_platform_id(session_ctx, current_message, platform_id)
        session_ctx.add_history_message(message=current_message)
        await session_ctx.broadcast_user_message(current_message)
        logger.info("OneBot 消息仅记录历史：回复策略未触发本轮响应（reason=%s）。", decision.reason)
        return

    if _should_reset_dialog(processed.internal, is_at, message_type):
        before_count = len(session_ctx.history)
        await _reset_session_context(session_ctx)
        reply_text = f"已重置对话，历史记录 {before_count} => 0"
        await session_ctx.send_assistant_message(reply_text)
        logger.info(f"OneBot 会话已重置: {session_ctx.session_id}，历史记录 {before_count} => 0")
        return

    await _fetch_and_cache_self_role(session_ctx, message_type, group_id, self_id, session_ctx.websocket)

    if session_ctx.session_id not in active_processors:
        try:
            processor = SessionProcessor(session_ctx, api_core.core_agent)
            await processor.start()
            active_processors[session_ctx.session_id] = processor
        except Exception as e:
            logger.error(f"启动会话处理器失败: {e}")
            return

    enriched_content = await _expand_reply_reference(processed.compressed, session_ctx)
    platform_id = event.get("message_id")
    proc = active_processors.get(session_ctx.session_id)
    metadata = {
        "message_parts": _enrich_reply_parts(processed.parts, session_ctx),
    }
    if proc and proc.has_pending_work():
        metadata.update({
            "interjection": True,
            "interjection_reason": "agent_running",
        })
    current_message = make_user_message(
        content=enriched_content,
        user_id=user_id,
        user_name=user_name,
        platform=platform,
        platform_id=platform_id,
        raw_content=processed.original,
        metadata=metadata,
    )
    bind_platform_id(session_ctx, current_message, platform_id)

    if message_type == "group" and not metadata.get("interjection"):
        # 先持久化以保证重启后不丢失（后续处理会跳过重复写入）
        session_ctx.add_history_message(message=current_message)

    await session_ctx.broadcast_user_message(current_message)
    await session_ctx.handle_new_message(current_message)


async def _handle_onebot_self_sent_event(
    event: Dict[str, Any],
    sender_factory: Callable[[SessionContext], Any],
    platform: str,
    log_cfg: Dict[str, Any],
) -> None:
    message_type = event.get("message_type")
    group_id = event.get("group_id")
    self_id = event.get("self_id") or event.get("user_id")
    user_id = str(event.get("user_id") or self_id or "onebot_bot")
    sender_info = event.get("sender") or {}
    bot_name = sender_info.get("nickname") or sender_info.get("card") or "OneBot Bot"
    raw_message = event.get("raw_message") or event.get("message") or ""
    message_id = event.get("message_id")

    session_id = build_session_id(
        message_type=message_type,
        user_id=user_id,
        group_id=group_id,
        self_id=str(self_id) if self_id is not None else None,
    )

    try:
        session_ctx = await get_or_create_session_context(
            session_id=session_id,
            user_id=user_id,
            user_name=bot_name,
            platform=platform,
            bot_name=None,
            history_dicts=None,
        )
    except Exception as e:
        logger.error(f"处理 OneBot 自身消息回显时创建会话上下文失败: {e}")
        return

    role = _cache_bot_group_role_from_event(event)
    if role:
        session_ctx.session_notes["self_role"] = role
        session_ctx.session_notes["onebot_last_self_sent"] = {
            "group_id": group_id,
            "self_id": self_id,
            "sender_role": role,
            "message_id": message_id,
            "time": event.get("time"),
        }
        logger.info(f"已从 Bot 自身消息回显缓存 Bot({self_id}) 在群({group_id}) 的权限: {role}")

    session_ctx.session_notes["onebot_target"] = {
        "message_type": message_type,
        "user_id": user_id,
        "group_id": group_id,
        "self_id": self_id,
        "sender_role": role or sender_info.get("role", "member"),
    }
    session_ctx.set_websocket(sender_factory(session_ctx))

    if message_id is not None:
        _bind_self_sent_platform_message_id(session_ctx, raw_message, message_id, platform, bot_name, user_id)

    metrics.track_adapter_message(
        platform,
        "out_echo",
        session_id,
        str(raw_message)[:1000],
    )

    if log_cfg.get("log_message", True):
        logger.info(
            "OneBot 收到 Bot 自身消息回显 [群:%s][Bot:%s][role:%s] message_id=%s: %s",
            group_id,
            self_id,
            role or sender_info.get("role", "unknown"),
            message_id,
            str(raw_message)[:200],
        )
    if log_cfg.get("debug_full_event", True):
        logger.debug(f"OneBot 自身消息回显完整事件: {json.dumps(event, ensure_ascii=False)}")


def _bind_self_sent_platform_message_id(
    session_ctx: SessionContext,
    raw_message: Any,
    message_id: Any,
    platform: str,
    bot_name: str,
    bot_user_id: str,
) -> None:
    raw_text = str(raw_message or "")

    for msg in reversed(session_ctx.history):
        if getattr(msg, "role", None) != "assistant":
            continue
        if getattr(msg, "platform_message_id", None):
            continue
        content = str(getattr(msg, "content", "") or "")
        if raw_text and content and (raw_text == content or raw_text in content or content in raw_text):
            session_ctx.set_platform_id_for_message(msg.message_id, message_id)
            logger.debug(
                "已通过 OneBot message_sent 回显绑定 assistant 消息平台 ID: message=%s platform=%s",
                msg.message_id,
                message_id,
            )
            return

    if raw_text:
        message = make_platform_context_message(
            role="assistant",
            content=raw_text,
            platform=platform,
            platform_id=message_id,
            metadata={
                "part_type": "send",
                "status": "completed",
            },
        )
        message.user_id = bot_user_id
        message.user_name = bot_name
        message.raw_content = raw_message
        session_ctx.add_history_message(message=message)


def _log_onebot_message(
    platform: str,
    message_type: Optional[str],
    user_id: str,
    user_name: str,
    group_id: Optional[int],
    self_id: Optional[str],
    processed: Any,
    event: Dict[str, Any],
    log_cfg: Dict[str, Any],
) -> None:
    if not log_cfg.get("log_message", True):
        return

    source_info = ""
    if message_type == "group" and group_id is not None:
        source_info = f"[群:{group_id}]"
    elif message_type == "private":
        source_info = f"[私聊]"
    else:
        source_info = f"[{message_type or '未知'}]"

    bot_info = f"[Bot:{self_id}]" if self_id else ""
    user_info = f"{user_name}({user_id})"
    
    compressed = getattr(processed, "compressed", None) or getattr(processed, "original", "")
    original = getattr(processed, "original", None)
    
    log_msg = f"OneBot 收到消息 {source_info}{bot_info} {user_info}: {compressed}"
    logger.info(log_msg)

    if log_cfg.get("debug_full_message", True) and original is not None:
        logger.debug(f"OneBot 收到消息(完整): {original}")
    if log_cfg.get("debug_full_event", True):
        logger.debug(f"OneBot 事件完整内容: {json.dumps(event, ensure_ascii=False)}")


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


def _should_reset_dialog(internal_content: str, is_mention: bool, message_type: Optional[str]) -> bool:
    is_private = (message_type or "").lower() == "private"
    if not is_private and not is_mention:
        return False
    if not internal_content:
        return False
    text = internal_content.lower().strip()
    keywords = ("重置对话", "重置聊天", "清理对话", "清理聊天", "清空对话", "清空聊天")
    return any(keyword in text for keyword in keywords)


async def _reset_session_context(session_ctx: SessionContext) -> None:
    session_ctx.clear_history()
    session_ctx.clear_pending_messages()
    session_ctx.clear_history_snapshot()
    session_ctx.clear_thought_process()
    if session_ctx.session_id in active_processors:
        processor = active_processors.pop(session_ctx.session_id)
        await processor.stop(abort_active=True)


async def _expand_reply_reference(content: str, session_ctx: SessionContext) -> str:
    pattern = r"\[reply,(?P<params>[^\]]+)\]"

    def parse_params(params_str: str) -> Dict[str, str]:
        params = {}
        for part in params_str.split(","):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            params[key.strip()] = value.strip()
        return params

    def extract_reply_id(params_str: str, parsed: Dict[str, str]) -> str:
        reply_id = parsed.get("id", "")
        if reply_id:
            return reply_id
        match = re.search(r"(?:^|,)\s*id=([^,]+)", params_str)
        return match.group(1).strip() if match else ""

    def sanitize_text(text: str) -> str:
        text = text.replace("\n", " ")
        text = text.replace("]", ")")
        text = text.replace(",", "，")
        return text

    async def fetch_snippet_from_platform(reply_id: str) -> Optional[str]:
        sender = session_ctx.websocket
        if not sender:
            return None
        params: Dict[str, Any] = {"message_id": int(reply_id)} if reply_id.isdigit() else {"message_id": reply_id}
        response = await onebot_action_tracker.request(sender, "get_msg", params, timeout=3.0)
        if not response or response.get("status") not in ("ok", "success"):
            return None
        data = response.get("data")
        if not isinstance(data, dict):
            return None
        raw_message = data.get("raw_message") or data.get("message")
        if not raw_message:
            return None
        try:
            message_processor = get_message_processor()
            processed = await message_processor.process_incoming_message(
                platform=session_ctx.platform or "onebot",
                raw_content=raw_message,
                message_data={"role": "user", "content": raw_message},
            )
            snippet = processed.compressed or processed.internal
        except Exception:
            snippet = str(raw_message)
        return sanitize_text(snippet)

    async def repl(match: re.Match) -> str:
        params_str = match.group("params") or ""
        params = parse_params(params_str)
        reply_id = extract_reply_id(params_str, params)
        if not reply_id:
            return "[消息获取失败]"
        target_internal_id: Optional[str] = None
        target_platform_id: Optional[str] = None
        target_msg = session_ctx.get_message_by_id(reply_id)
        if target_msg is not None:
            target_internal_id = reply_id
            target_platform_id = session_ctx.resolve_platform_message_id(reply_id)
        else:
            message_id = session_ctx.resolve_message_id_from_platform(reply_id)
            if message_id:
                target_internal_id = message_id
                target_platform_id = reply_id
                target_msg = session_ctx.get_message_by_id(message_id)
        if not target_msg or not getattr(target_msg, "content", None):
            fetched = await fetch_snippet_from_platform(reply_id)
            if not fetched:
                return f"[reply,id={reply_id},text=[消息获取失败]]"
            snippet = fetched
        else:
            raw_snippet = str(target_msg.content)
            try:
                message_processor = get_message_processor()
                compressed_snippet = await message_processor.content_compressor.compress(raw_snippet)
            except Exception:
                compressed_snippet = raw_snippet
            snippet = sanitize_text(compressed_snippet)
        if len(snippet) > 120:
            snippet = snippet[:120] + "..."
        if target_internal_id:
            params["id"] = str(target_internal_id)
            params["message_id"] = str(target_internal_id)
        if target_platform_id:
            params["platform_id"] = str(target_platform_id)
        params["text"] = snippet
        rebuilt = ",".join(f"{k}={v}" for k, v in params.items() if v)
        return f"[reply,{rebuilt}]"

    matches = list(re.finditer(pattern, content))
    if not matches:
        return content
    parts = []
    last_end = 0
    for match in matches:
        parts.append(content[last_end:match.start()])
        parts.append(await repl(match))
        last_end = match.end()
    parts.append(content[last_end:])
    return "".join(parts)


def _parse_group_command(content: str) -> Optional[Dict[str, Any]]:
    text = re.sub(r"\[at,[^\]]+\]", "", content or "")
    text = text.strip()
    if not text.startswith("/"):
        return None
    if text.startswith("/响应本群"):
        return {"type": "allow", "user": _extract_optional_user(text, "/响应本群")}
    if text.startswith("/忽略本群"):
        return {"type": "deny", "user": _extract_optional_user(text, "/忽略本群")}
    if text.startswith("/默认回复概率"):
        value = _extract_optional_user(text, "/默认回复概率")
        return {"type": "probability_default", "value": value}
    if text.startswith("/回复概率"):
        value = _extract_optional_user(text, "/回复概率")
        return {"type": "probability", "value": value}
    return None


def _extract_optional_user(text: str, prefix: str) -> Optional[str]:
    rest = text[len(prefix):].strip()
    if not rest:
        return None
    token = rest.split()[0]
    token = re.sub(r"\D", "", token)
    return token or None


async def _handle_group_command(
    command: Dict[str, Any],
    platform: str,
    bot_id: Optional[str],
    group_id: Optional[int],
) -> tuple[bool, str]:
    try:
        from app.config.policy_writer import update_whitelist_group, set_reply_probability
    except Exception as e:
        logger.error(f"加载策略写入器失败: {e}")
        return True, "配置更新失败（内部错误）"

    if group_id is None:
        if command["type"] != "probability_default":
            return True, "当前命令仅适用于群聊"

    if command["type"] == "allow":
        ok, reason = update_whitelist_group(
            platform=platform,
            bot_id=bot_id,
            group_id=str(group_id),
            action="allow",
            user_id=command.get("user"),
        )
        if ok:
            return True, "已设置：响应本群"
        return True, f"设置失败: {reason}"

    if command["type"] == "deny":
        ok, reason = update_whitelist_group(
            platform=platform,
            bot_id=bot_id,
            group_id=str(group_id),
            action="deny",
            user_id=command.get("user"),
        )
        if ok:
            return True, "已设置：忽略本群"
        return True, f"设置失败: {reason}"

    if command["type"] == "probability":
        value = command.get("value")
        if value is None:
            return True, "用法：/回复概率 0-100"
        try:
            value_int = int(value)
        except Exception:
            return True, "回复概率需为 0-100 的整数"
        ok, reason = set_reply_probability(
            platform=platform,
            bot_id=bot_id,
            group_id=str(group_id),
            probability=value_int,
        )
        if ok:
            return True, f"已设置：本群回复概率 {value_int}"
        return True, f"设置失败: {reason}"

    if command["type"] == "probability_default":
        value = command.get("value")
        if value is None:
            return True, "用法：/默认回复概率 0-100"
        try:
            value_int = int(value)
        except Exception:
            return True, "回复概率需为 0-100 的整数"
        ok, reason = set_reply_probability(
            platform=platform,
            bot_id=bot_id,
            group_id=None,
            probability=value_int,
        )
        if ok:
            return True, f"已设置：默认回复概率 {value_int}"
        return True, f"设置失败: {reason}"

    return False, ""

