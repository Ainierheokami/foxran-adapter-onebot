from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import websockets

from app.logger import setup_logger
from app.adapters.onebot_v11.config import onebot_v11_config
from app.adapters.onebot_v11.handlers import handle_onebot_event
from app.adapters.onebot_v11.senders import ForwardOneBotReplySender, parse_onebot_echo
from app.adapters.onebot_v11.action_tracker import onebot_action_tracker
from app.api.core import active_sessions
from app.utils.metrics_manager import metrics


logger = setup_logger(__name__)


class OneBotV11WsClient:
    def __init__(self):
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._connected: bool = False
        self._run_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._send_lock = asyncio.Lock()

    async def start(self) -> bool:
        cfg = onebot_v11_config.get_config()
        if not cfg.get("enabled"):
            logger.info("OneBot v11 WS 客户端已禁用：配置开关关闭")
            return False
        if cfg.get("connection_mode", "forward") not in ("forward", "both"):
            logger.info("OneBot v11 WS 客户端已禁用：连接模式不允许 forward")
            return False
        if self._run_task and not self._run_task.done():
            return True
        self._stop_event.clear()
        self._run_task = asyncio.create_task(self._run_loop())
        logger.info("OneBot v11 WS 客户端启动中")
        return True

    async def stop(self):
        self._stop_event.set()
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task
        await self._close_ws()
        self._run_task = None
        logger.info("OneBot v11 WS 客户端已停止")

    async def send_action(self, action: str, params: Dict[str, Any], echo: Optional[str] = None) -> bool:
        if not self._connected or not self._ws:
            logger.warning(f"OneBot 动作发送失败（未连接）: {action}")
            return False
        payload = {
            "action": action,
            "params": params,
            "echo": echo or str(uuid.uuid4()),
        }
        async with self._send_lock:
            try:
                await self._ws.send(json.dumps(payload, ensure_ascii=False))
                
                msg_content = ""
                if isinstance(params, dict) and "message" in params:
                    msg_content = str(params["message"])
                elif action != "send_msg" and action != "send_group_msg" and action != "send_private_msg":
                    msg_content = f"[{action}]"
                    
                if len(msg_content) > 1000:
                    msg_content = msg_content[:1000] + "..."
                    
                session_id, message_id = parse_onebot_echo(payload.get("echo"))
                if not (session_id and message_id):
                    metrics.track_adapter_message("onebot_v11", "out", "unknown_onebot_session", msg_content)
                return True
            except Exception as e:
                logger.error(f"发送 OneBot 动作失败: {action}, 错误: {e}")
                return False

    async def _run_loop(self):
        delay = onebot_v11_config.get_config().get("reconnect_initial_delay", 2.0)
        max_delay = onebot_v11_config.get_config().get("reconnect_max_delay", 30.0)

        while not self._stop_event.is_set():
            cfg = onebot_v11_config.get_config(force_reload=True)
            if not cfg.get("enabled"):
                await asyncio.sleep(1.0)
                continue
            if cfg.get("connection_mode", "forward") not in ("forward", "both"):
                await asyncio.sleep(1.0)
                continue

            try:
                await self._connect_and_listen(cfg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"OneBot WS 循环异常: {e}")

            if self._stop_event.is_set():
                break

            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)

    async def _connect_and_listen(self, cfg: Dict[str, Any]):
        ws_url = self._build_ws_url(cfg)
        headers = {}
        access_token = (cfg.get("access_token") or "").strip()
        if access_token and not cfg.get("access_token_in_url", True):
            headers["Authorization"] = f"Bearer {access_token}"

        ping_interval = cfg.get("ping_interval", 20.0)
        ping_timeout = cfg.get("ping_timeout", 10.0)

        logger.info(f"正在连接 OneBot v11 WS: {ws_url}")
        async with websockets.connect(
            ws_url,
            extra_headers=headers,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
        ) as ws:
            self._ws = ws
            self._connected = True
            logger.info("OneBot v11 WS 已连接")
            try:
                async for message in ws:
                    await self._handle_ws_message(message)
            finally:
                self._connected = False
                self._ws = None
                logger.warning("OneBot v11 WS 已断开")

    async def _handle_ws_message(self, message: Any):
        try:
            if isinstance(message, (bytes, bytearray)):
                message = message.decode("utf-8", errors="ignore")
            data = json.loads(message)
        except Exception:
            logger.debug("OneBot WS 收到非 JSON 消息")
            return

        if not isinstance(data, dict):
            return

        if "post_type" in data:
            await handle_onebot_event(
                data,
                sender_factory=lambda ctx: ForwardOneBotReplySender(self, ctx),
            )
            return

        if "status" in data and "echo" in data:
            logger.debug(f"OneBot 动作响应: {data.get('status')}, echo={data.get('echo')}")
            onebot_action_tracker.resolve(data.get("echo"), data)
            session_id, outgoing_message_id = parse_onebot_echo(data.get("echo"))
            platform_message_id = None
            data_block = data.get("data")
            if isinstance(data_block, dict):
                platform_message_id = data_block.get("message_id")
            if session_id and outgoing_message_id and platform_message_id is not None:
                session_ctx = active_sessions.get(session_id)
                if session_ctx:
                    session_ctx.set_platform_id_for_message(outgoing_message_id, platform_message_id)

    def _build_ws_url(self, cfg: Dict[str, Any]) -> str:
        ws_url = cfg.get("ws_url") or ""
        access_token = (cfg.get("access_token") or "").strip()
        if not access_token or not cfg.get("access_token_in_url", True):
            return ws_url

        parsed = urlparse(ws_url)
        query = dict(parse_qsl(parsed.query))
        if "access_token" not in query:
            query["access_token"] = access_token
        new_query = urlencode(query)
        return urlunparse(parsed._replace(query=new_query))

    async def _close_ws(self):
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        self._connected = False


onebot_v11_client = OneBotV11WsClient()

