import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import handlers as module


def test_mentioned_ids_lists_every_target_without_guessing_meaning():
    event = {
        "message": [
            {"type": "at", "data": {"qq": "third-party"}},
            {"type": "text", "data": {"text": "你好"}},
        ],
        "raw_message": "[CQ:at,qq=third-party] 你好",
    }

    assert module._extract_mentioned_ids(event) == ["third-party"]


def test_mention_of_self_matches_exact_id_only():
    assert module.is_mentioned({}, "[CQ:at,qq=10000] 你好", 10000)
    assert not module.is_mentioned({}, "[CQ:at,qq=100001] 你好", 10000)
    assert module.is_mentioned({"message": [{"type": "poke", "data": {"qq": "10000"}}]}, None, 10000)


def test_reply_platform_id_is_read_from_segments_or_cq_text():
    assert module._extract_reply_platform_id([{"type": "reply", "data": {"id": "-5"}}]) == "-5"
    assert module._extract_reply_platform_id("[CQ:reply,id=9001]继续") == "9001"
    assert module._extract_reply_platform_id("没有引用") is None
