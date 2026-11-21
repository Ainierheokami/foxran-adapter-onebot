"""Messages to one conversation are spaced apart and keep their order."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.inbound import ConversationRef
from app.outbound import OutboundMessage
from app.adapters.onebot_v11.network import senders
from app.adapters.onebot_v11.store.action_tracker import onebot_action_tracker


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
        answer = port.answer(params.get("message") or "")
        if answer is not None:
            asyncio.get_running_loop().call_soon(onebot_action_tracker.resolve, echo, {**answer, "echo": echo})
        return True

    port = senders.OneBotOutboundPort()
    port.sent = sent
    port.answer = lambda message: {"status": "ok", "retcode": 0, "data": {"message_id": 1}}
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


def test_a_video_goes_out_as_a_message_of_its_own():
    from app.message import MessageChain, Text, Video
    from app.message.segments import MediaRef

    session = SimpleNamespace(session_id="s1", log=SimpleNamespace(platform_id_of=lambda _id: None))
    message = senders.OneBotOutboundPort().encode(
        session, MessageChain([Text(text="捏好啦 "), Video(media=MediaRef(url="https://x/v.mp4")), Text(text="看看")])
    )

    assert message.payload == [("message", "捏好啦 "), ("media", "[CQ:video,file=https://x/v.mp4]"), ("message", "看看")]


def test_a_voice_from_a_url_is_sent_with_file():
    # OneBot v11 发语音只认 file；只写 url 的话实现端收不到音频
    from app.message import MessageChain, Voice
    from app.message.segments import MediaRef

    session = SimpleNamespace(session_id="s1", log=SimpleNamespace(platform_id_of=lambda _id: None))
    message = senders.OneBotOutboundPort().encode(
        session, MessageChain([Voice(media=MediaRef(url="https://x/api/media/cache/abc"))])
    )

    assert message.payload == [("media", "[CQ:record,file=https://x/api/media/cache/abc]")]


async def test_a_refused_send_is_reported_with_the_platform_reason(port):
    refuse = {"status": "failed", "retcode": 1200, "wording": 'message element "video" must be the only segment'}
    port.answer = lambda message: refuse if message.startswith("[CQ:video") else {"status": "ok", "retcode": 0}
    message = OutboundMessage(text="", payload=[("message", "好了"), ("media", "[CQ:video,file=https://x/v.mp4]")])

    with pytest.raises(senders.OneBotSendRejected, match="视频被平台拒收.*only segment.*1200"):
        await port.deliver(SESSION, GROUP, message, "m1")
    # 文字照样发了，视频也试了
    assert [text for _, _, text in port.sent] == ["好了", "[CQ:video,file=https://x/v.mp4]"]


async def test_no_answer_from_the_platform_is_not_a_failure(port, monkeypatch):
    monkeypatch.setattr(senders, "RESPONSE_WAIT_SECONDS", 0.05)
    port.answer = lambda message: None

    await port.deliver(SESSION, GROUP, _message("A"), "m1")
    assert not onebot_action_tracker._pending
