"""表情包解码成带 [动画表情] 摘要的图片段，宿主据此认出它是表情包。"""

from app.adapters.onebot_v11.adapter import OneBotAdapter
from app.message.segments import Image


def _decode(payload):
    return list(OneBotAdapter().from_platform_format(payload))


def test_sub_type_one_without_summary_is_a_sticker():
    [seg] = _decode([{"type": "image", "data": {"url": "https://x/a.gif", "sub_type": 1}}])
    assert isinstance(seg, Image) and seg.is_sticker


def test_plain_picture_is_not_a_sticker():
    [seg] = _decode([{"type": "image", "data": {"url": "https://x/a.jpg", "sub_type": 0, "summary": ""}}])
    assert not seg.is_sticker


def test_shop_sticker_keeps_its_name():
    [seg] = _decode([{"type": "mface", "data": {"url": "https://x/m.gif", "summary": "[哭哭]", "emoji_id": "1"}}])
    assert seg.is_sticker and seg.summary_text == "[动画表情:哭哭]"


def test_cq_sticker_by_sub_type():
    [seg] = _decode("[CQ:image,file=a.gif,sub_type=1,url=https://x/a.gif]")
    assert seg.is_sticker
