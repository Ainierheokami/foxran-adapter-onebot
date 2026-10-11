from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest

from tools import read_group_file as module


@pytest.fixture
def file_server():
    payload = {"body": b"hello zip"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload["body"])))
            self.end_headers()
            self.wfile.write(payload["body"])

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/ftn", payload
    server.shutdown()


@pytest.fixture
def group_session(monkeypatch, tmp_path):
    monkeypatch.setattr("app.tools.file_resolver.session_sandbox_root", lambda **_kwargs: tmp_path)
    return SimpleNamespace(
        session_id="onebot:1:group:748406347",
        websocket=object(),
        conversation=SimpleNamespace(group_id="748406347"),
    ), tmp_path


def _fake_platform(monkeypatch, url, calls):
    from app.adapters.onebot_v11.store.action_tracker import onebot_action_tracker

    async def fake_request(_sender, action, params, timeout=0):
        calls.append((action, params))
        return {"status": "ok", "data": {"url": url}}

    monkeypatch.setattr(onebot_action_tracker, "request", fake_request)


@pytest.mark.asyncio
async def test_group_file_is_fetched_by_file_id_into_sandbox_inbox(monkeypatch, file_server, group_session):
    url, _payload = file_server
    session_ctx, root = group_session
    calls = []
    _fake_platform(monkeypatch, url, calls)

    result = await module.ReadGroupFileTool().execute(
        file_id="/abc-123", file_name="亡者世界正式版.7z", session_ctx=session_ctx
    )

    assert result.success, result.error
    assert calls == [("get_group_file_url", {"group_id": 748406347, "file_id": "/abc-123"})]
    assert (root / "inbox" / "亡者世界正式版.7z").read_bytes() == b"hello zip"
    assert result.data["path"] == "inbox/亡者世界正式版.7z"


@pytest.mark.asyncio
async def test_declared_size_over_limit_is_refused_without_platform_call(monkeypatch, group_session):
    session_ctx, _root = group_session
    calls = []
    _fake_platform(monkeypatch, "http://unused", calls)

    result = await module.ReadGroupFileTool().execute(
        file_id="/abc", file_name="big.7z", size=module.MAX_FILE_BYTES + 1, session_ctx=session_ctx
    )

    assert not result.success
    assert "超过" in result.error
    assert calls == []


@pytest.mark.asyncio
async def test_download_over_limit_is_aborted_and_leaves_no_file(monkeypatch, file_server, group_session):
    url, payload = file_server
    payload["body"] = b"x" * 64
    session_ctx, root = group_session
    _fake_platform(monkeypatch, url, [])
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 16)

    result = await module.ReadGroupFileTool().execute(file_id="/abc", file_name="a.zip", session_ctx=session_ctx)

    assert not result.success
    assert "超过" in result.error
    assert list((root / "inbox").iterdir()) == []


def test_description_requires_explicit_user_request():
    description = module.ReadGroupFileTool.description
    assert "明确要求" in description
    assert "不要调用" in description


@pytest.mark.asyncio
async def test_oversize_download_is_refused_for_non_admin(monkeypatch, group_session):
    session_ctx, _root = group_session
    session_ctx.session_notes = {"current_user_id": "42"}
    calls = []
    _fake_platform(monkeypatch, "http://unused", calls)
    monkeypatch.setattr(module, "_is_admin", lambda _ctx: False)

    result = await module.ReadGroupFileTool().execute(
        file_id="/abc", file_name="big.7z", size=module.MAX_FILE_BYTES + 1, allow_oversize=True, session_ctx=session_ctx
    )

    assert not result.success
    assert "只有管理员" in result.error
    assert calls == []


@pytest.mark.asyncio
async def test_admin_can_download_oversize_file(monkeypatch, file_server, group_session):
    url, payload = file_server
    payload["body"] = b"x" * 64
    session_ctx, root = group_session
    _fake_platform(monkeypatch, url, [])
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 16)
    monkeypatch.setattr(module, "_is_admin", lambda _ctx: True)

    refused = await module.ReadGroupFileTool().execute(file_id="/abc", file_name="a.zip", session_ctx=session_ctx)
    allowed = await module.ReadGroupFileTool().execute(
        file_id="/abc", file_name="a.zip", allow_oversize=True, session_ctx=session_ctx
    )

    assert not refused.success and "allow_oversize=true" in refused.error
    assert allowed.success, allowed.error
    assert (root / "inbox" / "a.zip").stat().st_size == 64
    assert "不要整体读取" in allowed.data["hint"]


@pytest.mark.asyncio
async def test_admin_is_still_bound_by_hard_limit(monkeypatch, group_session):
    session_ctx, _root = group_session
    _fake_platform(monkeypatch, "http://unused", [])
    monkeypatch.setattr(module, "_is_admin", lambda _ctx: True)

    result = await module.ReadGroupFileTool().execute(
        file_id="/abc", file_name="huge.iso", size=module.ADMIN_MAX_FILE_BYTES + 1,
        allow_oversize=True, session_ctx=session_ctx,
    )

    assert not result.success
    assert "无法下载" in result.error


def test_admin_check_uses_current_speaker(monkeypatch):
    seen = {}

    def fake_is_admin(**kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setattr("app.adapters.control.policy.platform_policy.is_admin", fake_is_admin)
    ctx = SimpleNamespace(
        platform="onebot",
        session_notes={"current_user_id": "42"},
        conversation=SimpleNamespace(self_id="1", message_type="group", group_id="748406347"),
    )

    assert module._is_admin(ctx)
    assert seen["user_id"] == "42" and seen["group_id"] == "748406347"
    assert not module._is_admin(SimpleNamespace(session_notes={}, conversation=None))
