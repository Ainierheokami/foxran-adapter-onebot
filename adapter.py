from app.adapters.base.adapter import BasePlatformAdapter
from app.data_mappers.schemas import AtSchema, ImageSchema, PokeSchema, ReplySchema, VoiceSchema, FaceSchema, FileSchema, ForwardSchema, VideoSchema, MessageSegments

from typing import Any, List, Union, Optional

import re
from urllib.parse import urlparse
from app.logger import setup_logger

logger = setup_logger(__name__)

class OneBotAdapter(BasePlatformAdapter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        
        # 注册平台私有特权工具
        try:
            from app.tools.registry import tool_registry
            from app.adapters.onebot_v11.tools.read_forward_msg import ReadForwardMsgTool
            from app.adapters.onebot_v11.tools.kick import KickTool
            from app.adapters.onebot_v11.tools.ban import BanTool
            from app.adapters.onebot_v11.tools.poke import PokeTool
            from app.adapters.onebot_v11.tools.delete_msg import DeleteMsgTool
            from app.adapters.onebot_v11.tools.set_group_card import SetGroupCardTool
            
            tool_registry.register_tool_class(ReadForwardMsgTool)
            tool_registry.register_tool_class(KickTool)
            tool_registry.register_tool_class(BanTool)
            tool_registry.register_tool_class(PokeTool)
            tool_registry.register_tool_class(DeleteMsgTool)
            tool_registry.register_tool_class(SetGroupCardTool)
            
            logger.info("[OneBot适配器] 已在本地热加载平台专属私有特权工具组合: read_forward, kick, ban, poke, delete_msg, set_group_card")
        except Exception as e:
            logger.error(f"[OneBot适配器] 注册平台专属特权工具失败: {e}")


        # --- Regex for FROM_PLATFORM_FORMAT (Parsing OneBot CQ codes) ---
        # Input: "[CQ:at,qq=123]", "[CQ:image,url=...,summary=...]"
        # Output: AtSchema(id="123"), ImageSchema(url="...", summary="...")
        self._onebot_at_pattern = r"(?P<at_cq>\[CQ:at,qq=(?P<at_id>\d+?)(?:,name=(?P<at_name>[^,\]]*?))?\])"
        # 修复图片CQ码正则表达式，使用更灵活的方式解析参数
        self._onebot_image_pattern = \
            r"(?P<image_cq>\[CQ:image,(?P<image_params>[^\]]+)\])"
        self._onebot_poke_pattern = r"(?P<poke_cq>\[CQ:poke,qq=(?P<poke_id>\d+?)(?:,name=(?P<poke_name>[^,\]]*?))?\])"
        self._onebot_reply_pattern = r"(?P<reply_cq>\[CQ:reply,(?P<reply_params>[^\]]+)\])"
        self._onebot_record_pattern = r"(?P<record_cq>\[CQ:record,(?P<record_params>[^\]]+)\])"
        self._onebot_file_pattern = r"(?P<file_cq>\[CQ:file,(?P<file_params>[^\]]+)\])"
        self._onebot_video_pattern = r"(?P<video_cq>\[CQ:video,(?P<video_params>[^\]]+)\])"
        self._onebot_forward_pattern = r"(?P<forward_cq>\[CQ:forward,(?P<forward_params>[^\]]+)\])"
        self._onebot_face_pattern = r"(?P<face_cq>\[CQ:face,(?P<face_params>[^\]]+)\])"
        
        self._combined_onebot_cq_pattern = re.compile(
            f"{self._onebot_at_pattern}|"
            f"{self._onebot_image_pattern}|"
            f"{self._onebot_poke_pattern}|"
            f"{self._onebot_reply_pattern}|"
            f"{self._onebot_record_pattern}|"
            f"{self._onebot_file_pattern}|"
            f"{self._onebot_video_pattern}|"
            f"{self._onebot_forward_pattern}|"
            f"{self._onebot_face_pattern}"
        )

    def get_platform_prompts(self, session_ctx: Any) -> str:
        """动态向模型提供仅属于该适配器平台的特权环境信息"""
        target = session_ctx.session_notes.get("onebot_target", {})
        is_group = target.get("message_type") == "group"
        self_role = session_ctx.session_notes.get("self_role", "member")
        has_power = is_group and self_role in ("owner", "admin")
        
        prompts = (
            "### 平台环境说明\n"
            "当前处于 OneBot/QQ 通讯引擎下运作。\n"
        )
        if is_group:
            prompts += f"当前处于群聊环境，你的群内角色为: {self_role}。\n"
            if has_power:
                prompts += "【管理特权已开启】Bot 当前在本群具备管理员/群主权限，可在需要时调用管理工具协助维护群内秩序。\n"
            else:
                prompts += "注意：你当前为普通成员，部分敏感管理工具（如踢人、禁言等）可能因权限不足而无法生效。\n"
        else:
            prompts += "当前处于私聊环境。\n"
            
        return prompts

    def get_platform_tools(self, session_ctx: Any) -> list[str]:
        """动态返回当前会话下可用的平台专属特权工具名称列表"""
        target = session_ctx.session_notes.get("onebot_target", {})
        is_group = target.get("message_type") == "group"
        self_role = session_ctx.session_notes.get("self_role", "member")
        has_power = is_group and self_role in ("owner", "admin")

        tool_names = ["poke", "read_forward_msg"]
        if is_group:
            tool_names.extend(["delete_msg", "set_group_card"])
        if has_power:
            tool_names.extend(["kick", "ban"])
        return tool_names


    def _parse_params(self, params_str: str) -> dict:
        params = {}
        for part in params_str.split(","):
            if not part:
                continue
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            params[key.strip()] = value.strip()
        return params

    def from_platform_format(self, platform_data: Any) -> MessageSegments:
        if isinstance(platform_data, list):
            return self._from_segments(platform_data)
        if not isinstance(platform_data, str):
            if platform_data is None:
                return []
            try:
                platform_data = str(platform_data)
            except Exception:
                return [str(platform_data)]

        from app.logger import setup_logger
        logger = setup_logger(__name__)
        logger.debug(f"[OneBot适配器] 开始解析CQ码: {platform_data[:200]}...")

        segments = MessageSegments()
        last_end = 0
        parsed_segments_summary = []

        matches_found = list(self._combined_onebot_cq_pattern.finditer(platform_data))
        logger.debug(f"[OneBot适配器] 找到 {len(matches_found)} 个CQ码匹配")

        for i, match in enumerate(matches_found):
            match_start = match.start()
            if match_start > last_end:
                text_content = platform_data[last_end:match_start]
                if text_content.strip():
                    segments.append(text_content)
                    parsed_segments_summary.append("文本")
                    logger.debug(f"[OneBot适配器] 文本段 {i+1}: '{text_content[:50]}...'")

            if match.group("at_cq"):
                at_id = match.group("at_id")
                at_name = match.group("at_name") # Will be None if not present
                at_schema = AtSchema(id=at_id, name=at_name)
                segments.append(at_schema)
                parsed_segments_summary.append("@")
                logger.debug(f"[OneBot适配器] AT段 {i+1}: {at_schema}")

            elif match.group("image_cq"):
                # 解析图片CQ码参数
                params_str = match.group("image_params")
                logger.debug(f"[OneBot适配器] 图片段 {i+1} 参数: {params_str}")

                image_url = None
                image_summary = None

                # 提取url参数（改进版，支持URL和UUID）
                url_match = re.search(r'url=([^,\s]+?)(?=,|\s|$)', params_str)
                if url_match:
                    image_url = url_match.group(1)
                else:
                    fallback_match = re.search(r'url=([^\s,]+)', params_str)
                    if fallback_match:
                        image_url = fallback_match.group(1)
                logger.debug(f"[OneBot适配器] 图片URL: {image_url[:50] if image_url else 'N/A'}...")

                # 提取summary参数
                summary_match = re.search(r'(?:summary|ocr)=\[([^\]]*)\]', params_str)
                if summary_match:
                    image_summary = summary_match.group(1).strip()
                else:
                    summary_match = re.search(r'(?:summary|ocr)=([^,\]]*)', params_str)
                    if summary_match:
                        image_summary = summary_match.group(1).strip()
                logger.debug(f"[OneBot适配器] 图片summary: {image_summary}")

                image_schema = ImageSchema(url=image_url or "", summary=image_summary)
                segments.append(image_schema)
                parsed_segments_summary.append("图片")
                logger.debug(f"[OneBot适配器] 图片段解析完成: {image_schema}")

            elif match.group("poke_cq"):
                poke_id = match.group("poke_id")
                poke_name = match.group("poke_name") # Will be None
                poke_schema = PokeSchema(id=poke_id, name=poke_name)
                segments.append(poke_schema)
                parsed_segments_summary.append("戳一戳")
                logger.debug(f"[OneBot适配器] 戳一戳段 {i+1}: {poke_schema}")
            elif match.group("reply_cq"):
                params = self._parse_params(match.group("reply_params"))
                reply_id = params.get("id", "")
                reply_schema = ReplySchema(id=reply_id, platform_id=reply_id)
                segments.append(reply_schema)
                parsed_segments_summary.append("回复")
                logger.debug(f"[OneBot适配器] 回复段 {i+1}: {reply_schema}")
            elif match.group("record_cq"):
                params = self._parse_params(match.group("record_params"))
                voice_schema = VoiceSchema(
                    url=params.get("url"),
                    file=params.get("file"),
                    duration=self._safe_int(params.get("duration") or params.get("time")),
                )
                segments.append(voice_schema)
                parsed_segments_summary.append("语音")
                logger.debug(f"[OneBot适配器] 语音段 {i+1}: {voice_schema}")
            elif match.group("file_cq"):
                params = self._parse_params(match.group("file_params"))
                file_schema = FileSchema(
                    name=params.get("name"),
                    url=params.get("url"),
                    file=params.get("file"),
                    size=self._safe_int(params.get("size")),
                )
                segments.append(file_schema)
                parsed_segments_summary.append("文件")
                logger.debug(f"[OneBot适配器] 文件段 {i+1}: {file_schema}")
            elif match.group("video_cq"):
                params = self._parse_params(match.group("video_params"))
                video_schema = VideoSchema(
                    url=params.get("url"),
                    file=params.get("file"),
                    cover=params.get("cover"),
                )
                segments.append(video_schema)
                parsed_segments_summary.append("视频")
                logger.debug(f"[OneBot适配器] 视频段 {i+1}: {video_schema}")
            elif match.group("forward_cq"):
                params = self._parse_params(match.group("forward_params"))
                forward_schema = ForwardSchema(
                    id=params.get("id"),
                )
                segments.append(forward_schema)
                parsed_segments_summary.append("转发")
                logger.debug(f"[OneBot适配器] 转发段 {i+1}: {forward_schema}")
            elif match.group("face_cq"):
                params_str = match.group("face_params")
                # 因为 raw 里含有 JSON 字符串带有逗号，普通的 split(",") 会出问题，使用正则提取
                id_match = re.search(r'id=(\d+)', params_str)
                face_id = id_match.group(1) if id_match else "0"
                
                text_match = re.search(r"'faceText':\s*'([^']+)'", params_str)
                face_text = text_match.group(1) if text_match else None
                
                face_schema = FaceSchema(id=face_id, text=face_text)
                segments.append(face_schema)
                parsed_segments_summary.append("表情")
                logger.debug(f"[OneBot适配器] 表情段 {i+1}: {face_schema}")

            last_end = match.end()

        if last_end < len(platform_data):
            remaining_text = platform_data[last_end:]
            if remaining_text.strip():
                segments.append(remaining_text)
                parsed_segments_summary.append("文本")
                logger.debug(f"[OneBot适配器] 剩余文本: '{remaining_text[:50]}...'")

        summary_str = ", ".join(parsed_segments_summary)
        logger.debug(f"[OneBot适配器] ✅ CQ码解析完成: {len(segments.segments)} 个段落 ({summary_str})")
        logger.debug(f"[OneBot适配器] 最终结果: {str(segments)[:200]}...")
        
        return segments

    def _from_segments(self, segments_data: List[Any]) -> MessageSegments:
        segments = MessageSegments()
        for seg in segments_data:
            if not isinstance(seg, dict):
                segments.append(str(seg))
                continue
            seg_type = seg.get("type") or "text"
            data = seg.get("data") or {}
            if seg_type == "text":
                segments.append(str(data.get("text", "")))
            elif seg_type == "at":
                at_id = data.get("qq")
                at_name = data.get("name")
                segments.append(AtSchema(id=str(at_id), name=at_name))
            elif seg_type == "image":
                image_url = data.get("url") or data.get("file") or ""
                image_summary = data.get("summary") or data.get("ocr")
                segments.append(ImageSchema(url=str(image_url), summary=image_summary))
            elif seg_type == "poke":
                poke_id = data.get("qq")
                segments.append(PokeSchema(id=str(poke_id)))
            elif seg_type == "reply":
                reply_id = data.get("id") or data.get("message_id") or ""
                segments.append(ReplySchema(id=str(reply_id), platform_id=str(reply_id) if reply_id else None))
            elif seg_type in ("record", "voice"):
                voice_schema = VoiceSchema(
                    url=data.get("url"),
                    file=data.get("file"),
                    duration=self._safe_int(data.get("duration") or data.get("time")),
                )
                segments.append(voice_schema)
            elif seg_type == "file":
                file_schema = FileSchema(
                    name=data.get("name"),
                    url=data.get("url"),
                    file=data.get("file"),
                    size=self._safe_int(data.get("size")),
                )
                segments.append(file_schema)
            elif seg_type == "video":
                video_schema = VideoSchema(
                    url=data.get("url"),
                    file=data.get("file"),
                    cover=data.get("cover"),
                )
                segments.append(video_schema)
            elif seg_type == "forward":
                forward_schema = ForwardSchema(id=data.get("id"))
                segments.append(forward_schema)
            elif seg_type == "face":
                face_id = data.get("id")
                face_text = data.get("text")
                segments.append(FaceSchema(id=str(face_id), text=face_text))
            else:
                if isinstance(data, dict):
                    params = ",".join(f"{k}={v}" for k, v in data.items())
                    segments.append(f"[CQ:{seg_type},{params}]")
                else:
                    segments.append(f"[CQ:{seg_type}]")
        return segments

    def _safe_int(self, value: Any) -> Optional[int]:
        try:
            if value is None:
                return None
            return int(value)
        except Exception:
            return None

    def _schema_to_onebot_cq(self, schema_obj: Any) -> str:
        """Helper to convert a single internal schema object to OneBot CQ string."""
        if isinstance(schema_obj, AtSchema):
            params = [f"qq={schema_obj.id}"]
            if schema_obj.name: # 保留name参数以便正确转换回去
                params.append(f"name={schema_obj.name}")
            logger.debug(f"[OneBot适配器] AtSchema转换: id={schema_obj.id}, name={schema_obj.name}, 最终参数={params}")
            return f"[CQ:at,{','.join(params)}]"
        elif isinstance(schema_obj, ImageSchema):
            params = []
            image_url = schema_obj.url
            if image_url:
                image_url = self._maybe_proxy_url(image_url)
                # OneBot implementations commonly prefer file= for remote URLs
                params.append(f"file={image_url}")
                params.append(f"url={image_url}")
            if schema_obj.summary:
                params.append(f"summary={schema_obj.summary}")
            return f"[CQ:image,{','.join(params)}]"
        elif isinstance(schema_obj, PokeSchema):
            params = [f"qq={schema_obj.id}"]
            # if schema_obj.name: # Standard OneBot poke doesn't use 'name'
                # params.append(f"name={schema_obj.name}")
            return f"[CQ:poke,{','.join(params)}]"
        elif isinstance(schema_obj, ReplySchema):
            reply_id = schema_obj.platform_id or schema_obj.id
            if hasattr(self, "session_ctx") and getattr(self, "session_ctx", None):
                reply_id = self.session_ctx.resolve_platform_id(schema_obj.id) or reply_id
            return f"[CQ:reply,id={reply_id}]"
        elif isinstance(schema_obj, VoiceSchema):
            params = []
            if schema_obj.file:
                params.append(f"file={schema_obj.file}")
            if schema_obj.url:
                params.append(f"url={schema_obj.url}")
            if schema_obj.duration is not None:
                params.append(f"duration={schema_obj.duration}")
            return f"[CQ:record,{','.join(params)}]" if params else "[CQ:record]"
        elif isinstance(schema_obj, FileSchema):
            params = []
            if schema_obj.file:
                params.append(f"file={schema_obj.file}")
            if schema_obj.url:
                params.append(f"url={schema_obj.url}")
            if schema_obj.name:
                params.append(f"name={schema_obj.name}")
            if schema_obj.size is not None:
                params.append(f"size={schema_obj.size}")
            return f"[CQ:file,{','.join(params)}]" if params else "[CQ:file]"
        elif isinstance(schema_obj, VideoSchema):
            params = []
            video_source = schema_obj.file or schema_obj.url
            if video_source:
                video_source = self._maybe_proxy_url(video_source)
                # OneBot v11 uses `file` for outbound videos; `url` is receive-only.
                params.append(f"file={video_source}")
            if schema_obj.cover:
                params.append(f"cover={schema_obj.cover}")
            return f"[CQ:video,{','.join(params)}]" if params else "[CQ:video]"
        elif isinstance(schema_obj, ForwardSchema):
            if schema_obj.id:
                return f"[CQ:forward,id={schema_obj.id}]"
            return "[CQ:forward]"
        elif isinstance(schema_obj, FaceSchema):
            return f"[CQ:face,id={schema_obj.id}]"
        elif isinstance(schema_obj, str):
            return schema_obj
        else:
            return str(schema_obj) # Fallback

    def to_platform_format(self, internal_data: Any) -> str:
        logger.debug(f"[OneBot适配器] 开始平台格式转换: type={type(internal_data)}, data={str(internal_data)[:200]}...")
        
        from app.data_mappers.schemas import MessageSegments
        
        if isinstance(internal_data, (AtSchema, ImageSchema, PokeSchema, ReplySchema, VoiceSchema, FileSchema, ForwardSchema, VideoSchema, FaceSchema)):
            result = self._schema_to_onebot_cq(internal_data)
            logger.debug(f"[OneBot适配器] 对象转换结果: {result}")
            return result
        
        if isinstance(internal_data, (list, MessageSegments)):
            result = "".join(self._schema_to_onebot_cq(segment) for segment in internal_data)
            logger.debug(f"[OneBot适配器] 对象链转换结果: {result}")
            return result

        if isinstance(internal_data, str):
            # 经过AST重构后，这里基本不再出现包含格式标签的字符串。为了兼容处理纯字符串：
            logger.debug(f"[OneBot适配器] 纯字符串转换: {internal_data[:200]}...")
            return internal_data

        result = str(internal_data) # Fallback
        logger.debug(f"[OneBot适配器] Fallback转换: {result}")
        return result

    def _maybe_proxy_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return url
        # 已经是缓存地址则跳过
        if "/api/media/cache/" in url:
            return url
        try:
            from app.media.cache_store import cache_url
            entry = cache_url(url, check_update=False)
            access_url = entry.get("access_url") if isinstance(entry, dict) else None
            if access_url:
                return access_url
        except Exception as e:
            logger.warning(f"[OneBot适配器] 媒体缓存失败: {e}")
        return url
