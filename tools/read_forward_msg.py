from typing import Dict, Any, Optional
import html
import re
from typing import List, Tuple

from app.data_mappers.url_mapper import url_mapper
from app.tools.base import BaseTool, ToolType, ToolResult
from app.logger import setup_logger

logger = setup_logger(__name__)


_URL_REF_PATTERN = re.compile(
    r"^url-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _image_reference(data: Dict[str, Any]) -> str:
    """返回可由 image_understanding 直接读取的持久化图片引用。"""
    source = html.unescape(str(data.get("url") or data.get("file") or "").strip())
    if not source:
        return ""
    if _URL_REF_PATTERN.fullmatch(source):
        return source
    return url_mapper.shorten_url(source)


def _format_forward_segment(segment: Any) -> Tuple[str, List[str]]:
    if not isinstance(segment, dict):
        return str(segment), []
    type_ = str(segment.get("type") or "unknown").lower()
    data = segment.get("data") or {}
    if type_ == "text":
        return str(data.get("text", "")), []
    if type_ == "image":
        reference = _image_reference(data)
        summary = str(data.get("summary") or data.get("ocr") or "").strip()
        details = [f"image_identifier={reference}"] if reference else []
        if summary:
            details.append(f"summary={summary}")
        text = f"[图片,{', '.join(details)}]" if details else "[图片]"
        return text, [reference] if reference else []
    return f"[{type_}]", []


def _format_forward_content(content: Any) -> Tuple[str, List[str]]:
    """格式化转发内容，并把其中每张图片保留为 url-<uuid> 引用。"""
    if isinstance(content, list):
        text_parts = []
        image_identifiers = []
        for segment in content:
            text, references = _format_forward_segment(segment)
            text_parts.append(text)
            image_identifiers.extend(references)
        return "".join(text_parts), image_identifiers

    image_identifiers = []

    def replace_cq_image(match: re.Match) -> str:
        params = match.group("params")
        data: Dict[str, str] = {}
        for key in ("url", "file", "summary", "ocr"):
            value_match = re.search(rf"(?:^|,){key}=([^,\]]+)", params, flags=re.IGNORECASE)
            if value_match:
                data[key] = value_match.group(1).strip()
        text, references = _format_forward_segment({"type": "image", "data": data})
        image_identifiers.extend(references)
        return text

    text = re.sub(
        r"\[CQ:image,(?P<params>[^\]]+)\]",
        replace_cq_image,
        str(content or ""),
        flags=re.IGNORECASE,
    )
    return text, image_identifiers

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
            from app.adapters.onebot_v11.store.action_tracker import onebot_action_tracker
            
            # 使用 action_tracker 调用 OneBot API 获取转发内容
            response = await onebot_action_tracker.request(sender, "get_forward_msg", {"message_id": message_id}, timeout=8.0)
            
            if response and response.get("status") in ("ok", "success"):
                messages = response.get("data", {}).get("messages", [])
                content_buffer = []
                image_identifiers = []
                for m in messages:
                    sender = m.get("sender", {}).get("nickname", "未知")
                    content = m.get("content") or m.get("message", "")

                    content_str, content_images = _format_forward_content(content)
                    image_identifiers.extend(content_images)
                        
                    content_buffer.append(f"{sender}: {content_str}")
                
                final_content = "\n".join(content_buffer)
            else:
                final_content = f"转发记录读取失败或不存在 (ID: {message_id})"
                image_identifiers = []
                
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
            
            return ToolResult(
                self.name,
                True,
                {
                    "message": "合并转发记录已读取，并同步至上下文。",
                    "content": final_content,
                    "image_identifiers": list(dict.fromkeys(image_identifiers)),
                    "image_tool_hint": (
                        "如需识别这些图片，请将 image_identifiers 数组直接传给 image_understanding，"
                        "并通过 prompt 指定分析重点。"
                    ) if image_identifiers else "",
                },
            )
            
        except Exception as e:
            logger.error(f"读取转发消息失败: {e}")
            return ToolResult(self.name, False, error=f"读取过程中发生引擎异常: {e}")
