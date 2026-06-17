def register_adapter(registry):
    """注册 OneBot 适配器到平台注册表"""
    from .adapter import OneBotAdapter
    
    onebot_adapter = OneBotAdapter()
    registry.register_adapter('onebot', onebot_adapter)
    registry.register_adapter('qq', onebot_adapter)  # QQ平台使用OneBot
    
    # 注册自动检测规则
    registry.register_auto_detect(
        'onebot', 
        lambda p: 'qq' in p.lower() or 'onebot' in p.lower()
    )


async def startup():
    """Initialize adapter resources after loading."""


async def enable():
    """Start adapter-owned resources while enabled."""
    from .network.client import onebot_v11_client
    await onebot_v11_client.start()


async def disable():
    """Stop adapter-owned resources without uninstalling."""
    from .network.client import onebot_v11_client
    await onebot_v11_client.stop()


async def shutdown():
    """Release adapter-owned resources before a hot uninstall."""
    await disable()
