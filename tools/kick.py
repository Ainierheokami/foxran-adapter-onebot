from typing import Any, Optional
from pydantic import ConfigDict, Field

from app.tools.base import BaseTool, ToolInputSchema, ToolType, ToolResult
from app.logger import setup_logger

logger = setup_logger(__name__)

class KickInput(ToolInputSchema):
    # QQ 号常被写成 int；宿主 4.8.41 前的 ToolInputSchema 不会自动转字符串
    model_config = ConfigDict(coerce_numbers_to_str=True)
    user_id: str = Field(..., description="要踢出的用户ID（目标成员的QQ号或平台唯一标识）。")
    reject_add_request: bool = Field(False, description="拒绝此人的后续加群请求。默认为 false。")


class KickTool(BaseTool):
    output_kind = "data"
    # The action is visible in the group; the receipt (with member IDs) is for the agent.
    result_visibility = "agent_only"
    continuation_policy = "continue_agent"
    name: str = "kick"
    description: str = "踢出群聊中的某个成员。需要机器人是群主或管理员。仅在OneBot平台有效。"
    # 由于这不是需要读取结果进行再思考的动作，可以直接定义为 direct 或者 default (direct)
    tool_type: ToolType = "direct"
    
    args_schema = KickInput

    async def execute(self, user_id: str = "", reject_add_request: bool = False, session_ctx: Optional[Any] = None, **kwargs) -> ToolResult:
        if not session_ctx:
            return ToolResult(self.name, False, error="缺少会话上下文，无法执行踢人操作。")
            
        conversation = getattr(session_ctx, "conversation", None)
        group_id = conversation.group_id if conversation else None
        self_id = conversation.self_id if conversation else None
        
        if not group_id or not self_id:
            return ToolResult(self.name, False, error="当前不在群聊上下文中，无法踢人。")
            
        sender = getattr(session_ctx, "websocket", None)
        if not sender:
            return ToolResult(self.name, False, error="找不到当前活跃的通讯发射器(sender)，可能连接已断开，无法操作。")
            
        try:
            from app.adapters.onebot_v11.store.action_tracker import onebot_action_tracker
            from app.adapters.onebot_v11.tools.utils import verify_punish_permission

            # 进行二验（Bot 操作权限 vs 目标权限）
            permitted, reason = await verify_punish_permission(
                int(group_id), int(user_id), int(self_id), 
                onebot_action_tracker, sender,
                bot_role_hint=session_ctx.platform_state.get("self_role")
            )
            if not permitted:
                return ToolResult(self.name, False, reason)
            
            response = await onebot_action_tracker.request(
                sender, 
                "set_group_kick", 
                {
                    "group_id": int(group_id), 
                    "user_id": int(user_id), 
                    "reject_add_request": reject_add_request
                }, 
                timeout=5.0
            )
            
            if response and response.get("status") in ("ok", "success"):
                return ToolResult(self.name, True, f"踢人成功 (被踢ID: {user_id})")
            else:
                return ToolResult(self.name, False, error=f"踢人失败: {response.get('msg') if response else '未知错误'}")
                
        except Exception as e:
            logger.error(f"踢人动作发生异常: {e}")
            return ToolResult(self.name, False, error=f"执行踢人异常: {e}")
