import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import handlers as module
from app.adapters.onebot_v11.adapter import OneBotAdapter
from app.context.context_manager import Message
from app.message import Image, MediaRef, Mention, MessageChain


class _Session:
    def __init__(self, history):
        self.history = history
        self.bound = []
        self.added = []

    def set_platform_id_for_message(self, message_id, platform_id):
        self.bound.append((message_id, platform_id))

    def add_history_message(self, message):
        self.added.append(message)


def _processor():
    adapter = OneBotAdapter()
    return SimpleNamespace(platform_registry=SimpleNamespace(get_adapter=lambda _platform: adapter))


class SelfSentEchoTest(unittest.TestCase):
    def test_echo_with_mention_binds_to_segment_message(self):
        sent = Message(role="assistant", segments=MessageChain([Mention(target="42"), " 你好呀"]), internal_id="A1")
        session = _Session([sent])

        with patch.object(module, "get_message_processor", _processor):
            module._bind_self_sent_platform_message_id(session, "[CQ:at,qq=42] 你好呀", 9001, "onebot", "Bot", "10000")

        self.assertEqual(session.bound, [("A1", 9001)])
        self.assertEqual(session.added, [])

    def test_echo_with_rehosted_image_still_binds_by_text(self):
        sent = Message(
            role="assistant",
            segments=MessageChain(["画好啦", Image(media=MediaRef(url="/api/media/cache/abc"))]),
            internal_id="A2",
        )
        session = _Session([sent])

        with patch.object(module, "get_message_processor", _processor):
            module._bind_self_sent_platform_message_id(
                session, "画好啦[CQ:image,file=x.image,url=https://multimedia.nt.qq.com.cn/x]", 9002, "onebot", "Bot", "10000"
            )

        self.assertEqual(session.bound, [("A2", 9002)])

    def test_unmatched_echo_is_recorded_as_decoded_segments(self):
        session = _Session([])

        with patch.object(module, "get_message_processor", _processor):
            module._bind_self_sent_platform_message_id(session, "[CQ:at,qq=7] 手动发的", 9003, "onebot", "Bot", "10000")

        (message,) = session.added
        self.assertEqual([type(seg).__name__ for seg in message.segments], ["Mention", "Text"])
        self.assertEqual(message.platform_message_id, "9003")
        self.assertEqual(message.raw_content, "[CQ:at,qq=7] 手动发的")
