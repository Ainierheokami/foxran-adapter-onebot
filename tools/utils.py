from typing import Any, Dict, Optional, Tuple
import asyncio
from app.logger import setup_logger
from app.adapters.onebot_v11.role_store import (
    ROLE_TTL,
    VALID_GROUP_ROLES as _VALID_GROUP_ROLES,
    get_cached_bot_role as load_cached_bot_role,
    normalize_group_role,
)

logger = setup_logger(__name__)


def _role_to_power(role: Optional[str]) -> Optional[Tuple[int, str]]:
    if role == "owner":
        return 80, "群主"
    if role == "admin":
        return 50, "群管理"
    if role == "member":
        return 10, "成员"
    return None


def _get_cached_bot_role(group_id: int, bot_id: int) -> Optional[str]:
    try:
        from app.adapters.onebot_v11.handlers import _BOT_GROUP_ROLE_CACHE
        return load_cached_bot_role(_BOT_GROUP_ROLE_CACHE, group_id, bot_id, ttl=ROLE_TTL)
    except Exception:
        return None


def _extract_role_from_member_data(data: Any, target_user_id: int) -> Optional[str]:
    if isinstance(data, dict):
        role = normalize_group_role(data.get("role"))
        if role in _VALID_GROUP_ROLES:
            return role
        return None

    if not isinstance(data, list):
        return None

    target_id = str(target_user_id)
    for member in data:
        if not isinstance(member, dict):
            continue
        if str(member.get("user_id")) != target_id:
            continue
        role = normalize_group_role(member.get("role"))
        if role in _VALID_GROUP_ROLES:
            return role
        return None
    return None


async def fetch_group_member_role(
    group_id: int,
    target_user_id: int,
    action_tracker: Any,
    client: Any,
    *,
    no_cache: bool = False,
    timeout: float = 3.0,
) -> Optional[str]:
    """
    Fetch a member role through OneBot APIs.

    Some OneBot implementations occasionally fail or omit responses for
    get_group_member_info. get_group_member_list is heavier, but it is widely
    implemented and makes a useful fallback for infrequent permission checks.
    """
    api_attempts: tuple[tuple[str, Dict[str, Any]], ...] = (
        (
            "get_group_member_info",
            {"group_id": group_id, "user_id": target_user_id, "no_cache": no_cache},
        ),
        (
            "get_group_member_list",
            {"group_id": group_id, "no_cache": no_cache},
        ),
    )

    for action, params in api_attempts:
        try:
            response = await asyncio.wait_for(
                action_tracker.request(client, action, params, timeout=timeout),
                timeout=timeout + 0.5,
            )
        except Exception as e:
            logger.debug(f"获取群({group_id})成员({target_user_id})权限接口 {action} 异常: {e}")
            continue

        if not response or response.get("status") not in ("ok", "success"):
            logger.debug(f"获取群({group_id})成员({target_user_id})权限接口 {action} 无有效响应: {response}")
            continue

        role = _extract_role_from_member_data(response.get("data"), target_user_id)
        if role:
            if action != "get_group_member_info":
                logger.info(f"已通过备用接口 {action} 获取群({group_id})成员({target_user_id})权限: {role}")
            return role

        logger.debug(f"获取群({group_id})成员({target_user_id})权限接口 {action} 响应缺少 role: {response}")

    return None


async def get_user_power_level(
    group_id: int,
    target_user_id: int,
    bot_id: int,
    action_tracker,
    client,
    platform="onebot_v11",
    role_hint: Optional[str] = None,
) -> Tuple[int, str]:
    """
    获取指定用户在群内的操作特权等级
    100: Agent 超管 (从系统配置读取)
    80: 群主
    50: 群管理员
    10: 普通群员
    0: 未知或获取失败
    """
    # 1. 验证是否为 Bot 超管
    from app.adapters.control.policy import platform_policy
    if platform_policy.is_admin(
        platform=platform,
        bot_id=str(bot_id),
        user_id=str(target_user_id),
        message_type="group",
        group_id=group_id
    ):
        return 100, "超管"

    if int(target_user_id) == int(bot_id):
        hinted = role_hint if role_hint in _VALID_GROUP_ROLES else _get_cached_bot_role(group_id, bot_id)
        hinted_power = _role_to_power(hinted)
        if hinted_power:
            logger.info(f"使用回显/缓存中的 Bot 群权限进行二验: group={group_id}, bot={bot_id}, role={hinted}")
            return hinted_power

    # 2. 查平台接口验证 QQ 群身份
    try:
        role = await fetch_group_member_role(
            group_id,
            target_user_id,
            action_tracker,
            client,
            no_cache=True,
            timeout=3.0,
        )
        power = _role_to_power(role)
        if power:
            return power
    except Exception as e:
        logger.error(f"获取群({group_id})用户({target_user_id})全息权限异常: {e}")
        
    return 0, "未知"

async def verify_punish_permission(
    group_id: int,
    target_user_id: int,
    bot_id: int,
    action_tracker,
    client,
    bot_role_hint: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    进行二验：对比 Bot 与被执行目标的权限等级
    返回 (是否允许执行, 原因描述或日志)
    """
    bot_level, bot_role_name = await get_user_power_level(
        group_id,
        bot_id,
        bot_id,
        action_tracker,
        client,
        role_hint=bot_role_hint,
    )
    target_level, target_role_name = await get_user_power_level(group_id, target_user_id, bot_id, action_tracker, client)

    logger.info(f"二验对比: Bot[{bot_role_name} Lv{bot_level}] vs Target[{target_role_name} Lv{target_level}]")

    if target_level >= bot_level:
        return False, f"执行失败：被执行目标(【{target_role_name}】) 的权限等级大于等于 Bot执行权限(【{bot_role_name}】)，越权操作已被系统拦截。"
        
    if bot_level < 50:
         return False, "执行失败：Bot 在群内不具备管理权限 (可能刚被撤销)，无法执行此次操作。"

    return True, "认证通过"
