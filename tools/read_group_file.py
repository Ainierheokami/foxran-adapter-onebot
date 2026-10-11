from pathlib import Path
from typing import Any, Dict, Optional
import re

from app.tools.base import BaseTool, ToolType, ToolResult
from app.logger import setup_logger

logger = setup_logger(__name__)

# 群文件常见是整合包、安装包，下载前先按大小拦一道，避免把几百 MB 拉进沙盒。
MAX_FILE_BYTES = 50 * 1024 * 1024
# 超过上限时只有管理员可以要求照常下载；仍设一个硬上限，防止把服务器磁盘写满。
ADMIN_MAX_FILE_BYTES = 1024 * 1024 * 1024
INBOX_DIR = "inbox"

_URL_REF_PATTERN = re.compile(r"^url-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _resolve_reference(value: str) -> str:
    """上下文里较长的 file_id 会被压缩成 url-<uuid>，调用平台接口前还原。"""
    value = str(value or "").strip()
    if _URL_REF_PATTERN.fullmatch(value):
        from app.data_mappers.url_mapper import url_mapper

        return url_mapper.get_long_url(value) or value
    return value


def _safe_filename(name: str, fallback: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name or "")).strip(" .")
    return cleaned or fallback


def _is_admin(session_ctx: Any) -> bool:
    """当前发话人是否为 Agent 管理员（平台策略里配置的管理员，不是群管理员）。"""
    from app.adapters.control.policy import platform_policy

    conversation = getattr(session_ctx, "conversation", None)
    user_id = (getattr(session_ctx, "session_notes", None) or {}).get("current_user_id")
    if not user_id:
        return False
    return platform_policy.is_admin(
        platform=getattr(session_ctx, "platform", "onebot"),
        bot_id=getattr(conversation, "self_id", None) if conversation else None,
        user_id=str(user_id),
        message_type=getattr(conversation, "message_type", None) if conversation else "private",
        group_id=getattr(conversation, "group_id", None) if conversation else None,
    )


def _format_size(size: int) -> str:
    return f"{size / 1024 / 1024:.1f}MB" if size >= 1024 * 1024 else f"{size / 1024:.1f}KB"


class ReadGroupFileTool(BaseTool):
    output_kind = "data"
    result_visibility = "agent_only"
    continuation_policy = "continue_agent"
    name: str = "read_group_file"
    description: str = (
        "把聊天里别人发的群文件/私聊文件（上下文中的 [file,name=...,file=...] 标签）下载到当前会话沙盒的 inbox/ 目录，"
        "之后可用 session_sandbox_files 或 code_interpreter 读取、解压。"
        "只有用户明确要求你读取、下载、打开或分析某个文件时才调用；"
        "用户只是问“能不能看到文件”时不要调用，直接根据标签里的文件名和大小回答，并询问是否需要读取。"
        f"超过 {MAX_FILE_BYTES // 1024 // 1024}MB 的文件默认不下载：把大小告诉用户，让对方选择不读取，"
        "或由管理员明确要求照常下载，此时以 allow_oversize=true 再次调用（非管理员会被拒绝）。"
        "不要把文件地址写进 [image,...]。"
    )
    tool_type: ToolType = "perceptual"

    def get_input_schema_for_llm(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "文件标签里 file= 的值（平台文件凭据），不是 url= 的值。",
                },
                "file_name": {
                    "type": "string",
                    "description": "文件标签里 name= 的值，用作保存的文件名。",
                },
                "size": {
                    "type": "integer",
                    "description": "文件标签里 size= 的值（字节），没有就不填。",
                },
                "allow_oversize": {
                    "type": "boolean",
                    "description": "文件超过大小上限、且管理员明确要求照常下载时才设为 true。",
                },
            },
            "required": ["file_id"],
        }

    async def execute(
        self,
        file_id: str = "",
        file_name: str = "",
        size: Optional[int] = None,
        allow_oversize: bool = False,
        session_ctx: Optional[Any] = None,
        **kwargs,
    ) -> ToolResult:
        if not session_ctx or not getattr(session_ctx, "session_id", None):
            return ToolResult(self.name, False, error="缺少会话上下文，无法读取平台文件。")
        file_id = _resolve_reference(file_id)
        if not file_id:
            return ToolResult(self.name, False, error="缺少 file_id。请使用文件标签里 file= 的值。")
        limit = MAX_FILE_BYTES
        if allow_oversize:
            if not _is_admin(session_ctx):
                return ToolResult(
                    self.name, False,
                    error="只有管理员可以要求下载超过大小上限的文件。请告诉用户需要管理员确认，本次不下载。",
                )
            limit = ADMIN_MAX_FILE_BYTES
        if size and int(size) > limit:
            return self._too_large(file_name, int(size), limit)

        sender = getattr(session_ctx, "websocket", None)
        if not sender:
            return ToolResult(self.name, False, error="找不到当前活跃的通讯发射器(sender)，可能连接已断开，无法操作。")

        conversation = getattr(session_ctx, "conversation", None)
        group_id = getattr(conversation, "group_id", None) if conversation else None
        try:
            from app.adapters.onebot_v11.store.action_tracker import onebot_action_tracker

            if group_id:
                action, params = "get_group_file_url", {"group_id": int(group_id), "file_id": file_id}
            else:
                action, params = "get_private_file_url", {"file_id": file_id}
            response = await onebot_action_tracker.request(sender, action, params, timeout=10.0)
        except Exception as exc:
            logger.error(f"[群文件] 获取下载地址失败: {exc}", exc_info=True)
            return ToolResult(self.name, False, error=f"获取文件下载地址失败: {exc}")

        data = (response or {}).get("data") or {}
        url = data.get("url") if isinstance(data, dict) else None
        if not response or response.get("status") not in ("ok", "success") or not url:
            message = (response or {}).get("message") or (response or {}).get("wording") or "平台未返回下载地址"
            return ToolResult(self.name, False, error=f"获取文件下载地址失败: {message}（文件可能已过期或被删除）")

        from app.tools.file_resolver import resolve_session_sandbox_path, workspace_path_for_sandbox_path

        session_id = str(session_ctx.session_id)
        rel_path = f"{INBOX_DIR}/{_safe_filename(file_name, file_id.strip('/').replace('/', '_') or 'file.bin')}"
        target = resolve_session_sandbox_path(rel_path, session_id=session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            downloaded = await self._download(url, target, limit)
        except _TooLarge as exc:
            return self._too_large(file_name, exc.size, limit)
        except Exception as exc:
            logger.error(f"[群文件] 下载失败: {exc}", exc_info=True)
            return ToolResult(self.name, False, error=f"文件下载失败: {exc}")

        return ToolResult(self.name, True, {
            "message": f"文件已下载到会话沙盒，大小 {_format_size(downloaded)}。",
            "path": rel_path,
            "workspace_path": workspace_path_for_sandbox_path(rel_path, session_id=session_id),
            "size": downloaded,
            "hint": (
                "文件较大，已下载但不要整体读取：先用 code_interpreter 列出压缩包目录或查看部分内容，再按用户需要处理。"
                if downloaded > MAX_FILE_BYTES else
                "用 session_sandbox_files 读取文本文件；压缩包或二进制文件用 code_interpreter 在 /workspace 下处理。"
            ),
        })

    async def _download(self, url: str, target: Path, limit: int) -> int:
        import httpx

        partial = target.with_name(target.name + ".part")
        received = 0
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0)) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    declared = int(resp.headers.get("Content-Length") or 0)
                    if declared > limit:
                        raise _TooLarge(declared)
                    with open(partial, "wb") as fh:
                        async for chunk in resp.aiter_bytes(1024 * 1024):
                            received += len(chunk)
                            if received > limit:
                                raise _TooLarge(received)
                            fh.write(chunk)
            partial.replace(target)
            return received
        finally:
            partial.unlink(missing_ok=True)

    def _too_large(self, file_name: str, size: int, limit: int) -> ToolResult:
        if limit >= ADMIN_MAX_FILE_BYTES:
            advice = "已超过管理员可下载的硬上限，请告诉用户这个文件无法下载。"
        else:
            advice = (
                "请告诉用户文件超过上限，让对方选择：不读取；或由管理员明确要求照常下载"
                "（之后以 allow_oversize=true 再次调用）。"
            )
        return ToolResult(
            self.name,
            False,
            error=f"文件 {file_name or ''} 大小 {_format_size(size)}，超过 {_format_size(limit)} 上限，未下载。{advice}",
        )


class _TooLarge(Exception):
    def __init__(self, size: int):
        super().__init__(size)
        self.size = size
