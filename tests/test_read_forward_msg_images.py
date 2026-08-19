from types import SimpleNamespace

import pytest

from tools import read_forward_msg as module


def test_forward_list_content_preserves_image_as_guid(monkeypatch):
    monkeypatch.setattr(module.url_mapper, "shorten_url", lambda url: f"url-guid-for-{url.rsplit('/', 1)[-1]}")

    text, references = module._format_forward_content([
        {"type": "text", "data": {"text": "看图："}},
        {
            "type": "image",
            "data": {"url": "https://example.test/a.png", "summary": "测试图片"},
        },
    ])

    assert text == "看图：[图片,image_identifier=url-guid-for-a.png, summary=测试图片]"
    assert references == ["url-guid-for-a.png"]


def test_forward_cq_content_preserves_each_image_as_guid(monkeypatch):
    refs = iter(["url-guid-a", "url-guid-b"])
    monkeypatch.setattr(module.url_mapper, "shorten_url", lambda _url: next(refs))

    text, references = module._format_forward_content(
        "第一张[CQ:image,file=a,url=https://example.test/a.png]"
        "第二张[CQ:image,file=b,url=https://example.test/b.png]"
    )

    assert text == (
        "第一张[图片,image_identifier=url-guid-a]"
        "第二张[图片,image_identifier=url-guid-b]"
    )
    assert references == ["url-guid-a", "url-guid-b"]


def test_existing_url_reference_is_not_remapped(monkeypatch):
    existing = "url-a6a35e94-5649-fc6f-2b7d-41e4f6298dcf"

    def unexpected_call(_url):
        raise AssertionError("existing URL references must be reused")

    monkeypatch.setattr(module.url_mapper, "shorten_url", unexpected_call)
    text, references = module._format_forward_content([
        {"type": "image", "data": {"url": existing}},
    ])
    assert text == f"[图片,image_identifier={existing}]"
    assert references == [existing]


@pytest.mark.asyncio
async def test_tool_result_exposes_image_identifiers_for_followup_vision(monkeypatch):
    from app.adapters.onebot_v11.store.action_tracker import onebot_action_tracker

    async def fake_request(*_args, **_kwargs):
        return {
            "status": "ok",
            "data": {
                "messages": [{
                    "sender": {"nickname": "Alice"},
                    "content": [{
                        "type": "image",
                        "data": {"url": "https://example.test/forwarded.png"},
                    }],
                }],
            },
        }

    monkeypatch.setattr(onebot_action_tracker, "request", fake_request)
    monkeypatch.setattr(module.url_mapper, "shorten_url", lambda _url: "url-forwarded-image")

    history = []
    session = SimpleNamespace(
        websocket=object(),
        add_history_message=history.append,
    )
    result = await module.ReadForwardMsgTool().execute("forward-message", session_ctx=session)

    assert result.success is True
    assert result.data["image_identifiers"] == ["url-forwarded-image"]
    assert "image_understanding" in result.data["image_tool_hint"]
    assert "image_identifier=url-forwarded-image" in history[0].content
