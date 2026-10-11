from types import SimpleNamespace

import pytest

from app.inbound.events import ConversationRef
from app.media.files import FileUnavailable
from app.message import File, MediaRef
from network.file_resolver import onebot_file_resolver


def _session(scope="group", conv_id="748406347"):
    return SimpleNamespace(conversation=ConversationRef(platform="onebot", scope=scope, id=conv_id, account_id="874498833"))


def _fake_actions(monkeypatch, response):
    from app.adapters.onebot_v11.network.senders import onebot_outbound_port
    from app.adapters.onebot_v11.store.action_tracker import onebot_action_tracker

    calls = []

    async def fake_request(sender, action, params, timeout=0):
        calls.append((action, params))
        assert await sender.send_action(action, params) is True
        return response

    async def fake_send_action(account_id, action, params, echo=None):
        calls.append(("account", account_id))
        return True

    monkeypatch.setattr(onebot_action_tracker, "request", fake_request)
    monkeypatch.setattr(onebot_outbound_port, "send_action", fake_send_action)
    return calls


@pytest.mark.asyncio
async def test_group_file_is_resolved_by_file_id_on_the_conversation_account(monkeypatch):
    calls = _fake_actions(monkeypatch, {"status": "ok", "data": {"url": "https://signed.example/a.7z"}})
    segment = File(media=MediaRef(file="/abc-123"), name="a.7z", size=42)

    resolved = await onebot_file_resolver.resolve_file(_session(), segment)

    assert resolved.url == "https://signed.example/a.7z"
    assert (resolved.name, resolved.size) == ("a.7z", 42)
    assert calls[0] == ("get_group_file_url", {"group_id": 748406347, "file_id": "/abc-123"})
    assert ("account", "874498833") in calls


@pytest.mark.asyncio
async def test_private_file_uses_private_api(monkeypatch):
    calls = _fake_actions(monkeypatch, {"status": "ok", "data": {"url": "https://signed.example/b"}})

    await onebot_file_resolver.resolve_file(_session("private", "42"), File(media=MediaRef(file="/id"), name="b"))

    assert calls[0] == ("get_private_file_url", {"file_id": "/id"})


@pytest.mark.asyncio
async def test_failed_lookup_raises_file_unavailable(monkeypatch):
    _fake_actions(monkeypatch, {"status": "failed", "message": "file not found"})

    with pytest.raises(FileUnavailable, match="file not found"):
        await onebot_file_resolver.resolve_file(_session(), File(media=MediaRef(file="/gone"), name="x"))
