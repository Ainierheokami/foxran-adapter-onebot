from typing import Dict, Any, Optional
from app.tools.base import BaseTool, ToolType, ToolResult
from app.logger import setup_logger

logger = setup_logger(__name__)

class BanTool(BaseTool):
    name: str = "ban"
    description: str = "将群聊中的某个成员禁言指定时间。需要机器人是群主或管理员。仅在OneBot平台有效。"
    tool_type: ToolType = "direct"
    
    def get_input_schema_for_llm(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "要禁言的用户ID（目标成员的QQ号或平台唯一标识）。"
                },
                "duration": {
                    "type": "integer",
                    "description": "禁言的时长，单位为秒。0 表示解除禁言。默认为 1800 秒（30分钟）。",
                    "default": 1800
                }
            },
            "required": ["user_id"]
        }
        
    async def execute(self, user_id: str = "", duration: int = 1800, session_ctx: Optional[Any] = None, **kwargs) -> ToolResult:
        if not session_ctx:
            return ToolResult(self.name, False, error="缺少会话上下文，无法执行禁言操作。")
            
        target_info = session_ctx.session_notes.get("onebot_target", {})
        group_id = target_info.get("group_id")
        self_id = target_info.get("self_id")
        
        if not group_id or not self_id:
            return ToolResult(self.name, False, error="当前不在群聊上下文中，无法禁言。")
            
        sender = getattr(session_ctx, "websocket", None)
        if not sender:
            return ToolResult(self.name, False, error="找不到当前活跃的通讯发射器(sender)，可能连接已断开，无法操作。")
            
        try:
            from app.adapters.onebot_v11.action_tracker import onebot_action_tracker
            from app.adapters.onebot_v11.tools.utils import verify_punish_permission

            # 进行二验（Bot 操作权限 vs 目标权限）
            permitted, reason = await verify_punish_permission(
                int(group_id), int(user_id), int(self_id), 
                onebot_action_tracker, sender,
                bot_role_hint=session_ctx.session_notes.get("self_role")
            )
            if not permitted:
                return ToolResult(self.name, False, reason)
            
            response = await onebot_action_tracker.request(
                sender, 
                "set_group_ban", 
                {
                    "group_id": int(group_id), 
                    "user_id": int(user_id), 
                    "duration": duration
                }, 
                timeout=5.0
            )
            
            if response and response.get("status") in ("ok", "success"):
                action_str = "解除禁言" if duration == 0 else f"禁言 {duration}秒"
                return ToolResult(self.name, True, f"{action_str}成功 (目标ID: {user_id})")
            else:
                return ToolResult(self.name, False, error=f"操作失败: {response.get('msg') if response else '未知错误'}")
                
        except Exception as e:
            logger.error(f"禁言动作发生异常: {e}")
            return ToolResult(self.name, False, error=f"执行禁言异常: {e}")
