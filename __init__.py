from fastapi import APIRouter

try:
    from .adapter import OneBotAdapter
    from .api.endpoints import router as api_router
    from .network.reverse_ws import router as reverse_ws_router
except (ImportError, ValueError):
    from adapter import OneBotAdapter
    from api.endpoints import router as api_router
    from network.reverse_ws import router as reverse_ws_router

router = APIRouter()
router.include_router(api_router)
router.include_router(reverse_ws_router)


def _sync_echo_isolation():
    try:
        from app.adapters.outbound_tracker import load_echo_isolation_from_config
        try:
            from .config import onebot_v11_config
        except (ImportError, ValueError):
            from config import onebot_v11_config
        cfg = onebot_v11_config.get_config() if hasattr(onebot_v11_config, "get_config") else {}
        load_echo_isolation_from_config(owner="adapter:onebot", config=cfg)
    except Exception:
        pass


def register_adapter(registry):
    """注册 OneBot 适配器到平台注册表"""
    onebot_adapter = OneBotAdapter()
    registry.register_adapter('onebot', onebot_adapter)
    registry.register_adapter('qq', onebot_adapter)  # QQ平台使用OneBot
    
    # 注册自动检测规则
    registry.register_auto_detect(
        'onebot', 
        lambda p: 'qq' in p.lower() or 'onebot' in p.lower()
    )
    _sync_echo_isolation()


async def startup():
    """Initialize adapter resources after loading."""
    _sync_echo_isolation()


async def enable():
    """Start adapter-owned resources while enabled."""
    try:
        from .network.client import onebot_v11_client
    except (ImportError, ValueError):
        from network.client import onebot_v11_client
    await onebot_v11_client.start()
    _sync_echo_isolation()


async def disable():
    """Stop adapter-owned resources without uninstalling."""
    try:
        from .network.client import onebot_v11_client
    except (ImportError, ValueError):
        from network.client import onebot_v11_client
    await onebot_v11_client.stop()
    try:
        from app.adapters.outbound_tracker import get_outbound_tracker
        get_outbound_tracker().unregister_owner("adapter:onebot")
    except Exception:
        pass


async def shutdown():
    """Release adapter-owned resources before a hot uninstall."""
    await disable()
