"""Messages to one conversation are spaced apart and keep their order."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.inbound import ConversationRef
from app.outbound import OutboundMessage
from app.adapters.onebot_v11.network import senders


GROUP = ConversationRef(platform="onebot", scope="group", id="987", account_id="bot", self_id="999")
OTHER_GROUP = ConversationRef(platform="onebot", scope="group", id="654", account_id="bot", self_id="999")
SESSION = SimpleNamespace(session_id="s1")


@pytest.fixture
def port(monkeypatch):
    sent = []

    async def transport(action, params, echo=None):
        # A slow first call must not let a later message overtake it.
        if params.get("message") == "第一条":
            await asyncio.sleep(0.03)
        sent.append((time.monotonic(), params.get("group_id"), params.get("message")))
        return True

    port = senders.OneBotOutboundPort()
    port.sent = sent
    monkeypatch.setattr(port, "_transport", lambda account_id: transport)
    monkeypatch.setattr(senders, "_send_interval_seconds", lambda account_id: 0.1)
    return port


def _message(text):
    return OutboundMessage(text=text, payload=[("message", text)])


async def test_messages_to_one_group_keep_order_and_are_spaced(port):
    await asyncio.gather(*(
        port.deliver(SESSION, GROUP, _message(text), None)
        for text in ("第一条", "第二条", "第三条")
    ))

    assert [text for _, _, text in port.sent] == ["第一条", "第二条", "第三条"]
    gaps = [later - earlier for (earlier, _, _), (later, _, _) in zip(port.sent, port.sent[1:])]
    assert all(gap >= 0.09 for gap in gaps)


async def test_different_groups_are_not_held_back_by_each_other(port):
    started = time.monotonic()
    await asyncio.gather(
        port.deliver(SESSION, GROUP, _message("A"), None),
        port.deliver(SESSION, OTHER_GROUP, _message("B"), None),
    )

    assert time.monotonic() - started < 0.09


async def test_a_lone_message_after_a_pause_is_not_delayed(port):
    await port.deliver(SESSION, GROUP, _message("A"), None)
    await asyncio.sleep(0.12)
    started = time.monotonic()
    await port.deliver(SESSION, GROUP, _message("B"), None)

    assert time.monotonic() - started < 0.05


def test_interval_comes_from_the_account_and_zero_disables(monkeypatch):
    from app.adapters.onebot_v11.config import onebot_v11_config

    accounts = {"fast": {"send_interval_ms": 0}, "slow": {"send_interval_ms": 750}, "bad": {"send_interval_ms": "x"}}
    monkeypatch.setattr(onebot_v11_config, "get_account", lambda account_id: accounts.get(account_id, {}))

    assert senders._send_interval_seconds("fast") == 0
    assert senders._send_interval_seconds("slow") == 0.75
    assert senders._send_interval_seconds("bad") == senders.DEFAULT_SEND_INTERVAL_MS / 1000
    assert senders._send_interval_seconds("missing") == senders.DEFAULT_SEND_INTERVAL_MS / 1000
