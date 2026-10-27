from typing import Any, Optional
from pydantic import ConfigDict, Field

from app.tools.base import BaseTool, ToolInputSchema, ToolType, ToolResult
from app.logger import setup_logger

logger = setup_logger(__name__)

class PokeInput(ToolInputSchema):
    # QQ 号常被写成 int；宿主 4.8.41 前的 ToolInputSchema 不会自动转字符串
    model_config = ConfigDict(coerce_numbers_to_str=True)
    user_id: str = Field(..., description="要戳一戳的目标用户ID（QQ号）。")


class PokeTool(BaseTool):
    output_kind = "data"
    result_visibility = "user_visible"
    name: str = "poke"
    description: str = "戳一戳某个成员的面部或头像。可用于群聊中提醒或互动对方。"
    tool_type: ToolType = "direct"
    
    args_schema = PokeInput

    async def execute(self, user_id: str = "", session_ctx: Optional[Any] = None, **kwargs) -> ToolResult:
        if not session_ctx:
            return ToolResult(self.name, False, error="缺少会话上下文，无法执行操作。")
            
        conversation = getattr(session_ctx, "conversation", None)
        group_id = conversation.group_id if conversation else None
        
        if not group_id:
            return ToolResult(self.name, False, error="当前操作仅支持在群聊中戳一戳。")
            
        sender = getattr(session_ctx, "websocket", None)
        if not sender:
            return ToolResult(self.name, False, error="找不到当前活跃的通讯发射器(sender)，可能连接已断开，无法操作。")
            
        try:
            from app.adapters.onebot_v11.store.action_tracker import onebot_action_tracker
            
            response = await onebot_action_tracker.request(
                sender, 
                "group_poke", 
                {
                    "group_id": int(group_id), 
                    "user_id": int(user_id)
                }, 
                timeout=5.0
            )
            
            if response and response.get("status") in ("ok", "success"):
                return ToolResult(self.name, True)
            else:
                return ToolResult(self.name, False, error=f"戳一戳发送可能遇到问题，返回: {response.get('msg') if response else '未知错误'}。注意：部分客户端可能不支持该动作节点。")
                
        except Exception as e:
            logger.error(f"戳一戳动作发生异常: {e}")
            return ToolResult(self.name, False, error=f"执行戳一戳异常: {e}")
