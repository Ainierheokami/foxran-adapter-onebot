from typing import Any, Optional
from pydantic import ConfigDict, Field

from app.tools.base import BaseTool, ToolInputSchema, ToolType, ToolResult
from app.logger import setup_logger

logger = setup_logger(__name__)

class SetGroupCardInput(ToolInputSchema):
    # QQ 号常被写成 int；宿主 4.8.41 前的 ToolInputSchema 不会自动转字符串
    model_config = ConfigDict(coerce_numbers_to_str=True)
    user_id: str = Field(..., description="目标成员的QQ号或平台唯一标识（如果要改自己的，请填自己的ID）。")
    card: str = Field(..., description="要修改而成的新名片（空字符串表示删除名片，恢复原本昵称）。")


class SetGroupCardTool(BaseTool):
    output_kind = "data"
    # The action is visible in the group; the receipt (with member IDs) is for the agent.
    result_visibility = "agent_only"
    continuation_policy = "continue_agent"
    name: str = "set_group_card"
    description: str = "修改群内某个成员的群名片/群昵称。或者如果目标是你自己，可以修改自己的名片。"
    tool_type: ToolType = "direct"
    
    args_schema = SetGroupCardInput

    async def execute(self, user_id: str = "", card: str = "", session_ctx: Optional[Any] = None, **kwargs) -> ToolResult:
        if not session_ctx:
            return ToolResult(self.name, False, error="缺少会话上下文，无法执行操作。")
            
        conversation = getattr(session_ctx, "conversation", None)
        group_id = conversation.group_id if conversation else None
        self_id = conversation.self_id if conversation else None
        
        if not group_id or not self_id:
            return ToolResult(self.name, False, error="当前不在群聊上下文中，无法修改群名片。")
            
        sender = getattr(session_ctx, "websocket", None)
        if not sender:
            return ToolResult(self.name, False, error="找不到当前活跃的通讯发射器(sender)，可能连接已断开，无法操作。")
            
        try:
            from app.adapters.onebot_v11.store.action_tracker import onebot_action_tracker
            
            # 二验：如果是去改别人的名片，需要严格按照等级进行越权拦截
            if str(user_id) != str(self_id):
                from app.adapters.onebot_v11.tools.utils import verify_punish_permission
                permitted, reason = await verify_punish_permission(
                    int(group_id), int(user_id), int(self_id), 
                    onebot_action_tracker, sender,
                    bot_role_hint=session_ctx.platform_state.get("self_role")
                )
                if not permitted:
                    return ToolResult(self.name, False, error=f"修改名片失败：{reason}")
                    
            response = await onebot_action_tracker.request(
                sender, 
                "set_group_card", 
                {
                    "group_id": int(group_id), 
                    "user_id": int(user_id),
                    "card": card
                }, 
                timeout=5.0
            )
            
            if response and response.get("status") in ("ok", "success"):
                display_card = card if card else "<清除名片>"
                return ToolResult(self.name, True, f"修改名片成功 (目标ID: {user_id}, 新名片: {display_card})")
            else:
                return ToolResult(self.name, False, error=f"修改名片失败: {response.get('msg') if response else '未知错误'}")
                
        except Exception as e:
            logger.error(f"修改名片动作发生异常: {e}")
            return ToolResult(self.name, False, error=f"执行修改名片异常: {e}")
