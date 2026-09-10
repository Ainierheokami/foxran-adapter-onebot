from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.context.context_manager import Message
from tools.delete_msg import DeleteMsgTool


def _session_with_history():
    previous_request = Message(
        role="user",
        content="来个自拍",
        internal_id="request-1",
        user_id="123",
    )
    image = Message(
        role="assistant",
        content="[CQ:image,file=https://example.test/selfie.png]",
        internal_id="send:delivery-image",
        platform_message_id="456",
        metadata={"part_type": "send", "delivery_id": "delivery-image"},
    )
    current_request = Message(
        role="user",
        content="吓哭，你还是撤回吧",
        internal_id="request-2",
        user_id="123",
    )
    pre_delete_ack = Message(
        role="assistant",
        content="我来撤回啦",
        internal_id="send:delivery-ack",
        metadata={"part_type": "send", "delivery_id": "delivery-ack"},
    )
    history = [previous_request, image, current_request, pre_delete_ack]

    def get_by_internal(internal_id):
        return next(
            (message for message in history if message.internal_id == str(internal_id)),
            None,
        )

    def resolve_internal(platform_id):
        message = next(
            (message for message in history if message.platform_message_id == str(platform_id)),
            None,
        )
        return message.internal_id if message else None

    return SimpleNamespace(
        session_notes={
            "onebot_target": {
                "group_id": 987,
                "self_id": 999,
            }
        },
        websocket=object(),
        history=history,
        get_message_by_internal_id=get_by_internal,
        get_message_by_id=get_by_internal,
        resolve_internal_id_from_platform=resolve_internal,
        resolve_platform_id=lambda internal_id: next(
            (
                message.platform_message_id
                for message in history
                if message.internal_id == str(internal_id)
            ),
            None,
        ),
    )


@pytest.mark.asyncio
async def test_delete_own_message_by_internal_id_skips_admin_permission():
    session = _session_with_history()
    request = AsyncMock(
        side_effect=[
            {
                "status": "ok",
                "data": {"group_id": 987, "sender": {"user_id": 123}},
            },
            {"status": "ok", "data": {}},
        ]
    )

    with (
        patch(
            "app.adapters.onebot_v11.store.action_tracker.onebot_action_tracker.request",
            new=request,
        ),
        patch(
            "app.adapters.onebot_v11.tools.utils.verify_punish_permission",
            new=AsyncMock(return_value=(False, "should not be called")),
        ) as verify_permission,
    ):
        result = await DeleteMsgTool().execute(
            message_id="send:delivery-image",
            session_ctx=session,
        )

    assert result.success is True
    assert request.await_args_list[0].args[1:] == (
        "get_msg",
        {"message_id": 456},
    )
    assert request.await_args_list[1].args[1:] == (
        "delete_msg",
        {"message_id": 456},
    )
    verify_permission.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_stale_internal_id_falls_back_to_recent_own_resource():
    session = _session_with_history()
    request = AsyncMock(
        side_effect=[
            {
                "status": "ok",
                "data": {"group_id": 987, "sender": {"user_id": 999}},
            },
            {"status": "ok", "data": {}},
        ]
    )

    with patch(
        "app.adapters.onebot_v11.store.action_tracker.onebot_action_tracker.request",
        new=request,
    ):
        result = await DeleteMsgTool().execute(
            message_id="send:delivery-not-found",
            session_ctx=session,
        )

    assert result.success is True
    assert request.await_args_list[1].args[2] == {"message_id": 456}
