from typing import Dict, Any, Optional
from app.tools.base import BaseTool, ToolType, ToolResult
from app.logger import setup_logger

logger = setup_logger(__name__)

class DeleteMsgTool(BaseTool):
    name: str = "delete_msg"
    description: str = "撤回某条已经发出的消息。需要提供该消息的 message_id。如果你是群管/主，你可以撤回普通成员的消息。"
    tool_type: ToolType = "direct"
    
    def get_input_schema_for_llm(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "要撤回的消息ID。通常你可以从带有 id 标签的回调、或是你刚发送的日志记录中找到。"
                }
            },
            "required": ["message_id"]
        }
        
    async def execute(self, message_id: str = "", session_ctx: Optional[Any] = None, **kwargs) -> ToolResult:
        if not session_ctx:
            return ToolResult(self.name, False, error="缺少会话上下文，无法执行操作。")
            
        target_info = session_ctx.session_notes.get("onebot_target", {})
        group_id = target_info.get("group_id")
        self_id = target_info.get("self_id")
        
        sender = getattr(session_ctx, "websocket", None)
        if not sender:
            return ToolResult(self.name, False, error="找不到当前活跃的通讯发射器(sender)，可能连接已断开，无法操作。")
            
        try:
            from app.adapters.onebot_v11.action_tracker import onebot_action_tracker
            
            # Agent使用的是内部的 short GUID，尝试将其映射回平台真实的数字 message_id
            platform_id = session_ctx.resolve_platform_id(message_id)
            if platform_id:
                message_id = platform_id
                
            try:
                target_msg_id = int(message_id)
            except ValueError:
                return ToolResult(self.name, False, error=f"无效的消息ID格式: {message_id}。请提供有效的纯数字 message_id 或正确的上下文内部ID。")
            
            # 首先去获取这条消息的信息，确定发件人身份
            msg_info = await onebot_action_tracker.request(
                sender, 
                "get_msg", 
                {"message_id": target_msg_id}, 
                timeout=3.0
            )
            
            target_user_id = None
            msg_group_id = group_id
            
            if msg_info and msg_info.get("status") in ("ok", "success"):
                target_user_id = msg_info.get("data", {}).get("sender", {}).get("user_id")
                # 如果这个消息原本就不在群里，或者拿到了真实的 group_id，就使用真实的
                real_group_id = msg_info.get("data", {}).get("group_id")
                if real_group_id:
                    msg_group_id = real_group_id
            
            # 二验：如果要撤回的是别人的消息，并且在群内
            if msg_group_id and target_user_id and str(target_user_id) != str(self_id):
                from app.adapters.onebot_v11.tools.utils import verify_punish_permission
                permitted, reason = await verify_punish_permission(
                    int(msg_group_id), int(target_user_id), int(self_id), 
                    onebot_action_tracker, sender,
                    bot_role_hint=session_ctx.session_notes.get("self_role")
                )
                if not permitted:
                    return ToolResult(self.name, False, error=f"撤回失败（权限不足以撤回此成员的消息）：{reason}")
                    
            response = await onebot_action_tracker.request(
                sender, 
                "delete_msg", 
                {"message_id": target_msg_id}, 
                timeout=5.0
            )
            
            if response and response.get("status") in ("ok", "success"):
                return ToolResult(self.name, True, f"撤回消息成功 (消息ID: {message_id})")
            else:
                return ToolResult(self.name, False, error=f"撤回消息失败: {response.get('msg') if response else '未知错误'}")
                
        except Exception as e:
            logger.error(f"撤回动作发生异常: {e}")
            return ToolResult(self.name, False, error=f"执行撤回异常: {e}")
