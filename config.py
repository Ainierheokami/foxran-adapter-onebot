from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
import threading
import secrets
import re

from app.logger import setup_logger
from app.utils.yaml_utils import ensure_config_from_template, load_yaml_config_with_template
import yaml


logger = setup_logger(__name__)

ONEBOT_V11_CONFIG_PATH = Path("config/onebot_v11.yml")
ONEBOT_V11_TEMPLATE_PATH = Path(__file__).parent / "template.yml"

DEFAULT_ONEBOT_V11_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "connection_mode": "forward",
    "ws_url": "ws://127.0.0.1:8080",
    "access_token": "",
    "access_token_in_url": True,
    "platform_name": "onebot",
    "session_id_prefix": "onebot",
    "use_group_as_session": True,
    "session_id_include_bot_id": True,
    "reconnect_initial_delay": 2.0,
    "reconnect_max_delay": 30.0,
    "ping_interval": 20.0,
    "ping_timeout": 10.0,
    "logging": {
        "log_message": True,
        "log_notice": True,
        "log_request": True,
        "log_meta": True,
        "log_heartbeat": False,
        "debug_full_message": True,
        "debug_full_event": True,
    },
}


def _generate_token() -> str:
    return secrets.token_urlsafe(24)


def _ensure_access_token_in_file() -> Optional[str]:
    if not ONEBOT_V11_CONFIG_PATH.exists():
        return None
    try:
        with open(ONEBOT_V11_CONFIG_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        data = yaml.safe_load(content) or {}
        token_value = str((data.get("access_token") if isinstance(data, dict) else "") or "").strip()
        if token_value:
            return token_value
        new_token = _generate_token()
        token_re = re.compile(r"^(\s*access_token:\s*)(\".*?\"|'.*?'|[^#\n]*)", re.MULTILINE)
        if token_re.search(content):
            new_content = token_re.sub(rf"\\1\"{new_token}\"", content, count=1)
            with open(ONEBOT_V11_CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(new_content)
            return new_token
        return None
    except Exception as e:
        logger.error(f"自动生成 OneBot access_token 失败: {e}")
        return None


def _create_default_onebot_v11_config() -> None:
    try:
        created = ensure_config_from_template(ONEBOT_V11_CONFIG_PATH, ONEBOT_V11_TEMPLATE_PATH)
        if created:
            logger.info(f"Created OneBot v11 default config: {ONEBOT_V11_CONFIG_PATH}")
            _ensure_access_token_in_file()
    except Exception as e:
        logger.error(f"Failed to create OneBot v11 default config: {e}")


def load_onebot_v11_config() -> Dict[str, Any]:
    """Load OneBot v11 config without overwriting comments."""
    try:
        if not ONEBOT_V11_CONFIG_PATH.exists():
            _create_default_onebot_v11_config()
        cfg = load_yaml_config_with_template(
            ONEBOT_V11_CONFIG_PATH,
            ONEBOT_V11_TEMPLATE_PATH,
            DEFAULT_ONEBOT_V11_CONFIG,
        )
        updated_token = _ensure_access_token_in_file()
        if updated_token:
            cfg["access_token"] = updated_token
        return cfg
    except Exception as e:
        logger.error(f"Failed to load OneBot v11 config: {e}")
        return DEFAULT_ONEBOT_V11_CONFIG.copy()


class OneBotV11ConfigManager:
    """OneBot v11 config manager with basic caching."""

    def __init__(self):
        self._config: Optional[Dict[str, Any]] = None
        self._last_modified: float = 0.0
        self._lock = threading.Lock()

    def _get_file_mtime(self) -> float:
        try:
            if ONEBOT_V11_CONFIG_PATH.exists():
                return ONEBOT_V11_CONFIG_PATH.stat().st_mtime
        except Exception as e:
            logger.error(f"Failed to read OneBot v11 config mtime: {e}")
        return 0.0

    def _load_config_internal(self) -> Dict[str, Any]:
        return load_onebot_v11_config()

    def get_config(self, force_reload: bool = False) -> Dict[str, Any]:
        current_mtime = self._get_file_mtime()
        with self._lock:
            if (self._config is not None and not force_reload and
                    current_mtime <= self._last_modified):
                return self._config.copy()
            self._config = self._load_config_internal()
            self._last_modified = current_mtime
            logger.debug("OneBot v11 config loaded")
            return self._config.copy()

    def reload(self) -> bool:
        try:
            self.get_config(force_reload=True)
            logger.info("OneBot v11 config reloaded")
            return True
        except Exception as e:
            logger.error(f"Failed to reload OneBot v11 config: {e}")
            return False


onebot_v11_config = OneBotV11ConfigManager()
