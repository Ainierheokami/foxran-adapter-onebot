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
