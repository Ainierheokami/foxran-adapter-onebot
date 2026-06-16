from typing import Dict, Any, Optional
from app.tools.base import BaseTool, ToolType, ToolResult
from app.logger import setup_logger

logger = setup_logger(__name__)

class ReadForwardMsgTool(BaseTool):
    name: str = "read_forward_msg"
    description: str = "获取合并转发消息内的详细记录。只能读取平台特有的合并转发结构。"
    tool_type: ToolType = "perceptual"
    
    def get_input_schema_for_llm(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "需要读取的转发消息的 message_id，通常可以从 [收到聚合转发记录,id=xxx] 中提取得到。"
                }
            },
            "required": ["message_id"]
        }
        
    async def execute(self, message_id: str = "", session_ctx: Optional[Any] = None, **kwargs) -> ToolResult:
        if not session_ctx:
            return ToolResult(self.name, False, error="缺少会话上下文，无法读取平台数据。")
            
        sender = getattr(session_ctx, "websocket", None)
        if not sender:
            return ToolResult(self.name, False, error="找不到当前活跃的通讯发射器(sender)，可能连接已断开，无法操作。")
            
        try:
            from app.adapters.onebot_v11.action_tracker import onebot_action_tracker
            
            # 使用 action_tracker 调用 OneBot API 获取转发内容
            response = await onebot_action_tracker.request(sender, "get_forward_msg", {"message_id": message_id}, timeout=8.0)
            
            if response and response.get("status") in ("ok", "success"):
                messages = response.get("data", {}).get("messages", [])
                content_buffer = []
                for m in messages:
                    sender = m.get("sender", {}).get("nickname", "未知")
                    content = m.get("content") or m.get("message", "")
                    
                    if isinstance(content, list):
                        parts = []
                        for seg in content:
                            type_ = seg.get("type")
                            data_ = seg.get("data", {})
                            if type_ == "text": parts.append(data_.get("text", ""))
                            elif type_ == "image": parts.append("[图片]")
                            else: parts.append(f"[{type_}]")
                        content_str = "".join(parts)
                    else:
                        content_str = str(content)
                        
                    content_buffer.append(f"{sender}: {content_str}")
                
                final_content = "\n".join(content_buffer)
            else:
                final_content = f"转发记录读取失败或不存在 (ID: {message_id})"
                
            from app.adapters.message_protocol import make_platform_context_message

            info_msg = make_platform_context_message(
                role="system",
                platform="onebot",
                platform_id=message_id,
                content=f"【平台内部调用：合并记录读取完毕】\n目标ID: {message_id}\n\n[详细聊天记录]\n{final_content}",
                metadata={
                    "part_type": "tool_result",
                    "tool_name": self.name,
                    "tool_type": self.tool_type,
                    "status": "completed",
                    "hidden": True,
                },
            )
            session_ctx.add_history_message(info_msg)
            
            return ToolResult(self.name, True, "成功触发后台聚合阅读，记录以系统消息形式同步至你的上下文记忆。")
            
        except Exception as e:
            logger.error(f"读取转发消息失败: {e}")
            return ToolResult(self.name, False, error=f"读取过程中发生引擎异常: {e}")
