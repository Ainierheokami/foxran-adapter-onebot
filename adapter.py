from app.adapters.base.adapter import BasePlatformAdapter
from app.message import (
    ExtensionSegment, Face, File, Forward, Image, MediaRef, Mention, MessageChain,
    MessageRef, Poke, Reply, Segment, Text, Unknown, Video, Voice, segment_registry,
)

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
        # Output: Mention(target="123"), Image(media=MediaRef(url="..."), summary="...")
        self._onebot_at_pattern = r"(?P<at_cq>\[CQ:at,qq=(?P<at_id>\d+?)(?:,name=(?P<at_name>[^,\]]*?))?\])"
        # 修复图片CQ码正则表达式，使用更灵活的方式解析参数
        # A bracketed value such as summary=[动画表情] must not end the code early.
        self._onebot_image_pattern = \
            r"(?P<image_cq>\[CQ:image,(?P<image_params>(?:[^\[\]]|\[[^\]]*\])+)\])"
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
        conversation = getattr(session_ctx, "conversation", None)
        is_group = bool(conversation and conversation.is_group)
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
        conversation = getattr(session_ctx, "conversation", None)
        is_group = bool(conversation and conversation.is_group)
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

    def from_platform_format(self, platform_data: Any) -> MessageChain:
        if isinstance(platform_data, list):
            return self._from_segments(platform_data)
        if platform_data is None:
            return MessageChain()
        if not isinstance(platform_data, str):
            platform_data = str(platform_data)

        logger.debug(f"[OneBot适配器] 开始解析CQ码: {platform_data[:200]}...")
        segments = MessageChain()
        last_end = 0
        for match in self._combined_onebot_cq_pattern.finditer(platform_data):
            text_content = platform_data[last_end:match.start()]
            if text_content.strip():
                segments.append(text_content)
            segment = self._segment_from_cq(match)
            if segment is not None:
                segments.append(segment)
            last_end = match.end()

        remaining_text = platform_data[last_end:]
        if remaining_text.strip():
            segments.append(remaining_text)
        logger.debug(f"[OneBot适配器] CQ码解析完成: {len(segments)} 个段落")
        return segments

    def _segment_from_cq(self, match: re.Match) -> Optional[Segment]:
        if match.group("at_cq"):
            return Mention(target=match.group("at_id"), name=match.group("at_name"))
        if match.group("image_cq"):
            params_str = match.group("image_params")
            image_url = None
            url_match = re.search(r'url=([^,\s]+?)(?=,|\s|$)', params_str)
            if url_match:
                image_url = url_match.group(1)
            else:
                fallback_match = re.search(r'url=([^\s,]+)', params_str)
                if fallback_match:
                    image_url = fallback_match.group(1)
            image_summary = None
            summary_match = re.search(r'(?:summary|ocr)=(\[[^\]]*\])', params_str)
            if summary_match:
                image_summary = summary_match.group(1).strip()
            else:
                summary_match = re.search(r'(?:summary|ocr)=([^,\]]*)', params_str)
                if summary_match:
                    image_summary = summary_match.group(1).strip()
            return Image(media=MediaRef(url=image_url or ""), summary=image_summary)
        if match.group("poke_cq"):
            return Poke(target=match.group("poke_id"), name=match.group("poke_name"))
        if match.group("reply_cq"):
            params = self._parse_params(match.group("reply_params"))
            return Reply(ref=MessageRef(platform_message_id=params.get("id", "")))
        if match.group("record_cq"):
            params = self._parse_params(match.group("record_params"))
            return Voice(
                media=MediaRef(url=params.get("url"), file=params.get("file")),
                duration=self._safe_int(params.get("duration") or params.get("time")),
            )
        if match.group("file_cq"):
            params = self._parse_params(match.group("file_params"))
            return File(
                media=MediaRef(url=params.get("url"), file=params.get("file")),
                name=params.get("name"),
                size=self._safe_int(params.get("size")),
            )
        if match.group("video_cq"):
            params = self._parse_params(match.group("video_params"))
            cover = params.get("cover")
            return Video(
                media=MediaRef(url=params.get("url"), file=params.get("file")),
                cover=MediaRef(url=cover) if cover else None,
            )
        if match.group("forward_cq"):
            params = self._parse_params(match.group("forward_params"))
            return Forward(id=params.get("id"))
        if match.group("face_cq"):
            params_str = match.group("face_params")
            # raw 里含有带逗号的 JSON 字符串，不能直接 split(",")，用正则提取
            id_match = re.search(r'id=(\d+)', params_str)
            text_match = re.search(r"'faceText':\s*'([^']+)'", params_str)
            return Face(id=id_match.group(1) if id_match else "0", text=text_match.group(1) if text_match else None)
        return None

    def _from_segments(self, segments_data: List[Any]) -> MessageChain:
        segments = MessageChain()
        for seg in segments_data:
            if not isinstance(seg, dict):
                segments.append(str(seg))
                continue
            seg_type = seg.get("type") or "text"
            data = seg.get("data") or {}
            if seg_type == "text":
                segments.append(str(data.get("text", "")))
            elif seg_type == "at":
                segments.append(Mention(target=str(data.get("qq")), name=data.get("name")))
            elif seg_type == "image":
                image_url = data.get("url") or data.get("file") or ""
                segments.append(Image(media=MediaRef(url=str(image_url)), summary=data.get("summary") or data.get("ocr")))
            elif seg_type == "poke":
                segments.append(Poke(target=str(data.get("qq"))))
            elif seg_type == "reply":
                reply_id = data.get("id") or data.get("message_id") or ""
                segments.append(Reply(ref=MessageRef(platform_message_id=str(reply_id) if reply_id else None)))
            elif seg_type in ("record", "voice"):
                segments.append(Voice(
                    media=MediaRef(url=data.get("url"), file=data.get("file")),
                    duration=self._safe_int(data.get("duration") or data.get("time")),
                ))
            elif seg_type == "file":
                segments.append(File(
                    media=MediaRef(url=data.get("url"), file=data.get("file")),
                    name=data.get("name"),
                    size=self._safe_int(data.get("size")),
                ))
            elif seg_type == "video":
                cover = data.get("cover")
                segments.append(Video(
                    media=MediaRef(url=data.get("url"), file=data.get("file")),
                    cover=MediaRef(url=cover) if cover else None,
                ))
            elif seg_type == "forward":
                segments.append(Forward(id=data.get("id")))
            elif seg_type == "face":
                segments.append(Face(id=str(data.get("id")), text=data.get("text")))
            else:
                if isinstance(data, dict):
                    params = ",".join(f"{k}={v}" for k, v in data.items())
                    fallback_text = f"[CQ:{seg_type},{params}]"
                else:
                    fallback_text = f"[CQ:{seg_type}]"
                segments.append(Unknown(platform="onebot", raw=seg, fallback_text=fallback_text))
        return segments

    def _safe_int(self, value: Any) -> Optional[int]:
        try:
            if value is None:
                return None
            return int(value)
        except Exception:
            return None

    def _segment_to_cq(self, seg: Segment) -> str:
        """Convert one segment to OneBot CQ text."""
        if isinstance(seg, Text):
            return seg.text
        if isinstance(seg, Mention):
            params = [f"qq={seg.target}"]
            if seg.name:
                params.append(f"name={seg.name}")
            return f"[CQ:at,{','.join(params)}]"
        if isinstance(seg, Image):
            params = []
            image_url = seg.media.url
            if image_url:
                image_url = self._maybe_proxy_url(image_url)
                # OneBot implementations commonly prefer file= for remote URLs
                params.append(f"file={image_url}")
                params.append(f"url={image_url}")
            if seg.summary_text:
                params.append(f"summary={seg.summary_text}")
            return f"[CQ:image,{','.join(params)}]"
        if isinstance(seg, Poke):
            return f"[CQ:poke,qq={seg.target}]"
        if isinstance(seg, Reply):
            ref = seg.ref
            internal_id = ref.message_id if ref.message_id is not None else (ref.platform_message_id or "")
            reply_id = ref.platform_message_id or internal_id
            if getattr(self, "session_ctx", None):
                reply_id = self.session_ctx.resolve_platform_id(internal_id) or reply_id
            return f"[CQ:reply,id={reply_id}]"
        if isinstance(seg, Voice):
            params = []
            if seg.media.file:
                params.append(f"file={seg.media.file}")
            if seg.media.url:
                params.append(f"url={seg.media.url}")
            if seg.duration is not None:
                params.append(f"duration={seg.duration}")
            return f"[CQ:record,{','.join(params)}]" if params else "[CQ:record]"
        if isinstance(seg, File):
            params = []
            if seg.media.file:
                params.append(f"file={seg.media.file}")
            if seg.media.url:
                params.append(f"url={seg.media.url}")
            if seg.name:
                params.append(f"name={seg.name}")
            if seg.size is not None:
                params.append(f"size={seg.size}")
            return f"[CQ:file,{','.join(params)}]" if params else "[CQ:file]"
        if isinstance(seg, Video):
            params = []
            video_source = seg.media.file or seg.media.url
            if video_source:
                video_source = self._maybe_proxy_url(video_source)
                # OneBot v11 uses `file` for outbound videos; `url` is receive-only.
                params.append(f"file={video_source}")
            if seg.cover is not None and seg.cover.url:
                params.append(f"cover={seg.cover.url}")
            return f"[CQ:video,{','.join(params)}]" if params else "[CQ:video]"
        if isinstance(seg, Forward):
            return f"[CQ:forward,id={seg.id}]" if seg.id else "[CQ:forward]"
        if isinstance(seg, Face):
            return f"[CQ:face,id={seg.id}]"
        if isinstance(seg, Unknown):
            return seg.fallback_text
        if isinstance(seg, ExtensionSegment):
            return "".join(self._segment_to_cq(item) for item in segment_registry.fallback(seg))
        return seg.summary()

    def to_platform_format(self, internal_data: Any) -> str:
        if isinstance(internal_data, MessageChain):
            return "".join(self._segment_to_cq(seg) for seg in internal_data)
        if isinstance(internal_data, Segment):
            return self._segment_to_cq(internal_data)
        if isinstance(internal_data, str):
            return internal_data
        return str(internal_data)

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
