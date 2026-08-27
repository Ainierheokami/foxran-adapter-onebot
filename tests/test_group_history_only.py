import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import handlers as module


class _SessionContextStub:
    def __init__(self):
        self.session_id = "onebot:874498833:group:200924263"
        self.session_notes = {}
        self.history = []
        self.broadcasts = []
        self.handled = []

    def add_history_message(self, message):
        self.history.append(message)

    async def broadcast_user_message(self, message):
        self.broadcasts.append(message)

    async def handle_new_message(self, message):
        self.handled.append(message)


class GroupHistoryOnlyTest(unittest.IsolatedAsyncioTestCase):
    async def test_non_triggering_group_image_is_recorded_without_starting_llm(self):
        image_url = "https://multimedia.nt.qq.com.cn/download?fileid=test-image"
        processed = SimpleNamespace(
            internal=f"[image,url={image_url}]",
            compressed=f"[image,url={image_url}]",
            original=f"[CQ:image,file=4411AB.jpg,url={image_url}]",
            parts=[{"type": "image", "url": image_url}],
        )

        async def process_incoming_message(**_kwargs):
            return processed

        processor = SimpleNamespace(process_incoming_message=process_incoming_message)
        session = _SessionContextStub()

        async def get_session(**_kwargs):
            return session

        decision = SimpleNamespace(
            allowed=True,
            should_reply=False,
            reason="probability_miss",
        )
        event = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 200924263,
            "self_id": 874498833,
            "user_id": 744988353,
            "message_id": 2112847190,
            "sender": {"nickname": "赫缇斯喵狼", "role": "owner"},
            "message": [{"type": "image", "data": {"url": image_url}}],
            "raw_message": processed.original,
        }

        with (
            patch.object(module.api_core, "core_agent", object()),
            patch.object(module, "get_message_processor", return_value=processor),
            patch.object(module, "get_or_create_session_context", side_effect=get_session),
            patch.object(module.platform_policy, "evaluate", return_value=decision),
            patch.object(module.metrics, "track_adapter_message"),
            patch.object(module, "bind_platform_id"),
        ):
            await module.handle_onebot_event(event, sender_factory=lambda _session: object())

        self.assertEqual(
            [message.content for message in session.history],
            [processed.compressed],
        )
        self.assertEqual(session.broadcasts, session.history)
        self.assertEqual(session.handled, [])
        self.assertNotIn(session.session_id, module.active_processors)


if __name__ == "__main__":
    unittest.main()
