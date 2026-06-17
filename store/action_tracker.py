from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, Optional

from app.logger import setup_logger


logger = setup_logger(__name__)


class OneBotActionTracker:
    def __init__(self) -> None:
        self._pending: Dict[str, asyncio.Future] = {}

    async def request(self, sender: Any, action: str, params: Dict[str, Any], timeout: float = 3.0) -> Optional[Dict[str, Any]]:
        if not sender or not hasattr(sender, "send_action"):
            return None
        echo = f"action:{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[echo] = future
        ok = await sender.send_action(action, params, echo=echo)
        if not ok:
            self._pending.pop(echo, None)
            return None
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            if isinstance(result, dict):
                return result
            return None
        except asyncio.TimeoutError:
            logger.debug(f"OneBot 动作响应超时: {action}")
            self._pending.pop(echo, None)
            return None

    def resolve(self, echo: Any, data: Any) -> None:
        if not echo:
            return
        future = self._pending.pop(str(echo), None)
        if future and not future.done():
            future.set_result(data)


onebot_action_tracker = OneBotActionTracker()
