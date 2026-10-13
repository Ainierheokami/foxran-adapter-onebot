"""OneBot recall capability used by the framework's ``delete_msg`` tool."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.inbound import ConversationRef
from app.outbound import RecallRefused
from app.adapters.onebot_v11.network import senders


CONVERSATION = ConversationRef(platform="onebot", scope="group", id="987", account_id="bot", self_id="999")
SESSION = SimpleNamespace(platform_state={"self_role": "member"})


@pytest.fixture
def port(monkeypatch):
    sent = AsyncMock(return_value=True)
    port = senders.OneBotOutboundPort()
    monkeypatch.setattr(port, "_transport", lambda account_id: sent if account_id == "bot" else None)
    return port


@pytest.mark.asyncio
async def test_own_message_is_recalled_without_permission_check(port):
    with patch("app.adapters.onebot_v11.store.action_tracker.onebot_action_tracker.request", new_callable=AsyncMock) as request:
        request.return_value = {"status": "ok"}
        await port.recall(SESSION, CONVERSATION, "456", own=True)

    assert [call.args[1:3] for call in request.await_args_list] == [("delete_msg", {"message_id": 456})]


@pytest.mark.asyncio
async def test_other_members_message_needs_group_power(port):
    responses = {
        "get_msg": {"status": "ok", "data": {"sender": {"user_id": 123}, "group_id": 987}},
        "delete_msg": {"status": "ok"},
    }
    with patch(
        "app.adapters.onebot_v11.store.action_tracker.onebot_action_tracker.request",
        new=AsyncMock(side_effect=lambda sender, action, params, timeout: responses[action]),
    ), patch(
        "app.adapters.onebot_v11.tools.utils.verify_punish_permission",
        new=AsyncMock(return_value=(False, "Bot 是普通成员")),
    ):
        with pytest.raises(RecallRefused, match="权限不足"):
            await port.recall(SESSION, CONVERSATION, "456", own=False)


@pytest.mark.asyncio
async def test_message_the_platform_reports_as_ours_skips_the_permission_check(port):
    responses = {
        "get_msg": {"status": "ok", "data": {"sender": {"user_id": 999}, "group_id": 987}},
        "delete_msg": {"status": "ok"},
    }
    check = AsyncMock()
    with patch(
        "app.adapters.onebot_v11.store.action_tracker.onebot_action_tracker.request",
        new=AsyncMock(side_effect=lambda sender, action, params, timeout: responses[action]),
    ), patch("app.adapters.onebot_v11.tools.utils.verify_punish_permission", new=check):
        await port.recall(SESSION, CONVERSATION, "456", own=False)

    check.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnected_account_and_bad_ids_are_refused(port):
    with pytest.raises(RecallRefused, match="未连接"):
        await port.recall(SESSION, ConversationRef(platform="onebot", scope="group", id="1", account_id="other"), "1", own=True)
    with pytest.raises(RecallRefused, match="无效"):
        await port.recall(SESSION, CONVERSATION, "send:abc", own=True)
