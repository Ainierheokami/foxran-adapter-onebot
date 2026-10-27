import asyncio
import time
from types import SimpleNamespace

import pytest

from app.adapters.onebot_v11 import handlers
from app.adapters.onebot_v11.store import role_store


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(role_store, "DB_PATH", tmp_path / "roles.db")
    monkeypatch.setattr(role_store, "_db_ready", False)
    monkeypatch.setattr(role_store, "_persisted", {})
    monkeypatch.setattr(handlers, "_BOT_GROUP_ROLE_CACHE", {})
    monkeypatch.setattr(handlers, "_BOT_GROUP_ROLE_FAILURES", {})
    monkeypatch.setattr(handlers, "_BOT_GROUP_ROLE_LOCKS", {})


def _session():
    return SimpleNamespace(platform_state={})


def _api(monkeypatch, result):
    calls = []

    async def fetch(*args, **kwargs):
        calls.append(args)
        return result

    from app.adapters.onebot_v11.tools import utils

    monkeypatch.setattr(utils, "fetch_group_member_role", fetch)
    return calls


def test_admin_notice_updates_role_without_api(monkeypatch):
    calls = _api(monkeypatch, None)
    handlers._remember_role_from_notice(
        {"notice_type": "group_admin", "sub_type": "set", "group_id": 1, "self_id": 9, "user_id": 9}
    )
    session = _session()
    asyncio.run(handlers._fetch_and_cache_self_role(session, "group", 1, 9, None))
    assert session.platform_state["self_role"] == "admin"
    assert calls == []

    # Someone else being promoted says nothing about the bot.
    handlers._remember_role_from_notice(
        {"notice_type": "group_admin", "sub_type": "unset", "group_id": 1, "self_id": 9, "user_id": 5}
    )
    assert handlers._BOT_GROUP_ROLE_CACHE[(1, 9)][0] == "admin"


def test_failed_api_keeps_last_known_role_and_backs_off(monkeypatch):
    calls = _api(monkeypatch, None)
    role_store.persist_bot_role(1, 9, "owner")
    stale = time.time() - role_store.ROLE_TTL - 10
    with role_store.connect_sqlite(role_store.DB_PATH) as conn:
        conn.execute("UPDATE onebot_bot_group_roles SET updated_at = ?", (stale,))

    session = _session()
    asyncio.run(handlers._fetch_and_cache_self_role(session, "group", 1, 9, None))
    assert session.platform_state["self_role"] == "owner"
    assert len(calls) == 1

    asyncio.run(handlers._fetch_and_cache_self_role(session, "group", 1, 9, None))
    assert len(calls) == 1  # Still cooling down.

    handlers._BOT_GROUP_ROLE_FAILURES[(1, 9)] = (1, time.time() - 61)
    asyncio.run(handlers._fetch_and_cache_self_role(session, "group", 1, 9, None))
    assert len(calls) == 2
    assert handlers._BOT_GROUP_ROLE_FAILURES[(1, 9)][0] == 2
    assert role_store.unknown_cooldown(2) == 120


def test_repeated_echo_role_is_not_rewritten(monkeypatch):
    writes = []
    original = role_store.persist_bot_role

    def counting(*args, **kwargs):
        writes.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(role_store, "persist_bot_role", counting)
    cache = {}
    for _ in range(3):
        role_store.remember_bot_role(cache, 1, 9, "admin")
    assert len(writes) == 1
    role_store.remember_bot_role(cache, 1, 9, "member")
    assert len(writes) == 2
