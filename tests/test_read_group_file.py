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


@pytest.fixture
def slow_file_server():
    import threading

    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "20")
            self.end_headers()
            self.wfile.write(b"a" * 10)
            self.wfile.flush()
            release.wait(5)
            self.wfile.write(b"b" * 10)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/slow", release
    release.set()
    server.shutdown()


class _Session(SimpleNamespace):
    def __init__(self, root, **kwargs):
        super().__init__(
            session_id="onebot:1:group:748406347",
            websocket=object(),
            conversation=SimpleNamespace(group_id="748406347"),
            sent=[],
            background={},
            **kwargs,
        )

    def register_background_task(self, task_id, description):
        self.background[task_id] = "running"

    def complete_background_task(self, task_id, result, status="completed"):
        self.background[task_id] = status

    async def send_assistant_message(self, text, **_kwargs):
        self.sent.append(text)
        return True


@pytest.mark.asyncio
async def test_progress_is_reported_to_status_callback(monkeypatch, file_server, group_session):
    url, payload = file_server
    payload["body"] = b"x" * 100
    session_ctx, _root = group_session
    _fake_platform(monkeypatch, url, [])
    logs = []

    async def status_callback(status, output):
        logs.append((status, output))

    result = await module.ReadGroupFileTool().execute(
        file_id="/abc", file_name="a.zip", session_ctx=session_ctx, status_callback=status_callback
    )

    assert result.success and result.data["status"] == "completed"
    assert {status for status, _ in logs} == {"log"}
    assert "开始下载" in logs[0][1]
    assert any("100%" in output for _, output in logs)
    assert "下载完成" in logs[-1][1]


@pytest.mark.asyncio
async def test_slow_download_moves_to_background_and_notifies(monkeypatch, slow_file_server, tmp_path):
    import asyncio

    url, release = slow_file_server
    monkeypatch.setattr("app.tools.file_resolver.session_sandbox_root", lambda **_kwargs: tmp_path)
    monkeypatch.setattr(module, "SYNC_WAIT_SECONDS", 0.3)
    _fake_platform(monkeypatch, url, [])
    session_ctx = _Session(tmp_path)
    tool = module.ReadGroupFileTool()

    first = await tool.execute(file_id="/abc", file_name="big.7z", session_ctx=session_ctx)
    duplicate = await tool.execute(file_id="/abc", file_name="big.7z", session_ctx=session_ctx)

    assert first.success and first.data["status"] == "downloading"
    assert duplicate.success and duplicate.data["status"] == "downloading"
    assert "不要重复调用" in duplicate.data["message"]
    assert session_ctx.background == {"read_group_file:inbox/big.7z": "running"}
    assert session_ctx.sent == []

    release.set()
    for _ in range(50):
        if session_ctx.sent:
            break
        await asyncio.sleep(0.05)

    assert (tmp_path / "inbox" / "big.7z").read_bytes() == b"a" * 10 + b"b" * 10
    assert session_ctx.background == {"read_group_file:inbox/big.7z": "completed"}
    assert len(session_ctx.sent) == 1 and "下载完成" in session_ctx.sent[0]
    assert module._ACTIVE_DOWNLOADS == {}
