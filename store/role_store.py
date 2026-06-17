from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from app.config.db_config import DB_DIR
from app.logger import setup_logger
from app.utils.sqlite_utils import connect_sqlite

logger = setup_logger(__name__)

VALID_GROUP_ROLES = {"owner", "admin", "member"}
ROLE_TTL = 600.0
UNKNOWN_COOLDOWN = 60.0
DB_PATH = Path(DB_DIR) / "onebot_role_cache.db"

BotRoleCache = Dict[tuple[int, int], tuple[str, float]]


def normalize_group_role(role) -> Optional[str]:
    role_text = str(role or "").lower()
    return role_text if role_text in VALID_GROUP_ROLES else None


def _ensure_db() -> None:
    with connect_sqlite(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS onebot_bot_group_roles (
                group_id INTEGER NOT NULL,
                self_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                updated_at REAL NOT NULL,
                source TEXT,
                PRIMARY KEY (group_id, self_id)
            )
            """
        )
        conn.commit()


def persist_bot_role(group_id: int, self_id: int, role: str, *, source: str = "unknown") -> None:
    normalized = normalize_group_role(role)
    if not normalized:
        return
    _ensure_db()
    now = time.time()
    with connect_sqlite(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO onebot_bot_group_roles (group_id, self_id, role, updated_at, source)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(group_id, self_id) DO UPDATE SET
                role = excluded.role,
                updated_at = excluded.updated_at,
                source = excluded.source
            """,
            (int(group_id), int(self_id), normalized, now, source),
        )
        conn.commit()


def load_bot_role(group_id: int, self_id: int) -> Optional[Tuple[str, float]]:
    _ensure_db()
    with connect_sqlite(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT role, updated_at
            FROM onebot_bot_group_roles
            WHERE group_id = ? AND self_id = ?
            """,
            (int(group_id), int(self_id)),
        ).fetchone()
    if not row:
        return None
    role = normalize_group_role(row[0])
    if not role:
        return None
    return role, float(row[1] or 0)


def remember_bot_role(
    cache: BotRoleCache,
    group_id: int,
    self_id: int,
    role: str,
    *,
    source: str = "memory",
    persist: bool = True,
) -> Optional[str]:
    normalized = normalize_group_role(role)
    if not normalized:
        return None
    cache[(int(group_id), int(self_id))] = (normalized, time.time())
    if persist:
        try:
            persist_bot_role(int(group_id), int(self_id), normalized, source=source)
        except Exception as e:
            logger.warning("持久化 OneBot Bot 群权限缓存失败: %s", e)
    return normalized


def get_cached_bot_role(
    cache: BotRoleCache,
    group_id: int,
    self_id: int,
    *,
    ttl: float = ROLE_TTL,
    load_persistent: bool = True,
) -> Optional[str]:
    cache_key = (int(group_id), int(self_id))
    now = time.time()
    cached = cache.get(cache_key)
    if cached:
        role, ts = cached
        if role in VALID_GROUP_ROLES and now - ts < ttl:
            return role

    if not load_persistent:
        return None

    try:
        persisted = load_bot_role(int(group_id), int(self_id))
    except Exception as e:
        logger.warning("读取 OneBot Bot 群权限持久化缓存失败: %s", e)
        return None
    if not persisted:
        return None

    role, ts = persisted
    if now - ts > ttl:
        return None
    cache[cache_key] = (role, ts)
    return role
