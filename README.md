# Foxran Adapter: OneBot v11

本插件为 Foxran Agent 提供 [OneBot v11](https://github.com/botuniverse/onebot-11) 协议标准的适配支持，使得智能体可以直接接入主流的即时通讯软件（如 QQ）的机器人框架（如 NapCatQQ、Lagrange 等）。

## 📦 安装与挂载

推荐使用 Foxran WebUI 的 **插件市场 (Marketplace)** 进行一键安装。

若需手动安装，在 Foxran Agent 根目录下运行：
```bash
python scripts/install_market_plugin.py https://github.com/Foxran/foxran-adapter-onebot.git --type adapter
```

## ⚙️ 配置说明

安装完成并启动一次 Foxran Agent 后，插件会自动在你的主项目 `config/` 目录下生成 `onebot_v11.yml` 配置文件。

核心配置项说明：

```yaml
enabled: true                  # 是否开启本适配器
connection_mode: "forward"     # 默认采用正向 WebSocket 模式连接
ws_url: "ws://127.0.0.1:8080"  # OneBot 提供端的 WebSocket 服务地址
access_token: "xxx"            # 连接鉴权 Token（插件首次运行会自动随机生成以确保安全）
```

*(修改配置后支持系统热重载，无需重启核心服务)*

## 🛠 内置特有能力

为适配 IM 场景，本插件内嵌了针对群聊管控的特定 Tools：
- **群组管理**: 包含禁言 (`ban`)、踢出群聊 (`kick`)、修改群名片 (`set_group_card`) 等。
- **互动行为**: 提供双击头像戳一戳 (`poke`)、读取合并转发记录 (`read_forward_msg`) 等接口。

以上工具当且仅当智能体通过 OneBot 协议端收到消息时可用，不污染全局 WebUI 沙盒空间。
