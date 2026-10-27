import asyncio

import pytest

from app.adapters.onebot_v11 import handlers
from app.adapters.onebot_v11.store.action_tracker import onebot_action_tracker


class Sender:
    def __init__(self):
        self.echoes = []

    async def send_action(self, action, params, echo=None):
        self.echoes.append(echo)
        return True


@pytest.mark.asyncio
async def test_an_api_reply_reaches_a_handler_still_running(monkeypatch):
    # The reply to an API call arrives on the same socket as the events. The
    # receive loop must keep reading while an event's handler waits for it.
    sender = Sender()
    replies = []

    async def handler(event, account_id="default"):
        replies.append(await onebot_action_tracker.request(sender, "get_group_member_info", {}, timeout=2.0))

    monkeypatch.setattr(handlers, "handle_onebot_event", handler)
    await asyncio.wait_for(
        handlers.dispatch_onebot_event({"post_type": "message", "group_id": 1}), timeout=0.5,
    )
    while not sender.echoes:
        await asyncio.sleep(0)
    onebot_action_tracker.resolve(sender.echoes[0], {"status": "ok", "data": {"role": "admin"}})
    await handlers._dispatcher.drain()
    assert replies == [{"status": "ok", "data": {"role": "admin"}}]


def test_events_are_keyed_by_conversation():
    assert handlers._event_key({"group_id": 5}, "a") == "a:group:5"
    assert handlers._event_key({"user_id": 7}, "a") == "a:private:7"
    assert handlers._event_key({"post_type": "meta_event"}, "a") == "a:other"
