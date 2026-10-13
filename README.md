# Foxran Adapter: OneBot v11

本插件为 Foxran Agent 提供 [OneBot v11](https://github.com/botuniverse/onebot-11) 协议标准的适配支持，使得智能体可以直接接入主流的即时通讯软件（如 QQ）的机器人框架（如 NapCatQQ、Lagrange 等）。

## 📦 安装与挂载

推荐使用 Foxran WebUI 的 **插件市场 (Marketplace)** 进行一键安装。

若需手动安装，在 Foxran Agent 根目录下运行：
```bash
python scripts/install_market_plugin.py https://github.com/Ainierheokami/foxran-adapter-onebot.git --type adapter
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
- **互动行为**: 提供双击头像戳一戳 (`poke`)、读取合并转发记录 (`read_forward_msg`) 等接口。读取转发记录时会保留其中图片的 `url-<uuid>` 引用，并返回 `image_identifiers`，可直接交给宿主的 `image_understanding` 做多图分析。

以上工具当且仅当智能体通过 OneBot 协议端收到消息时可用，不污染全局 WebUI 沙盒空间。

撤回消息由宿主的框架工具 `delete_msg` 负责（定位消息、判断是否为 Bot 自己发出）；本适配器在出站端口上实现 `recall`，撤回其他成员的消息时比较群权限。

`/stop`、`/reset`、`停止`、`重置对话` 等控制命令会在进入 LLM 前执行。群聊中需要 @ 机器人或由管理员发送；命令采用完整匹配，普通对话中提到这些词不会误触发。

工具已声明结构化执行契约：群管和互动操作返回用户可见的 `data` 结果；读取合并转发属于 Agent 内部数据，并要求宿主在读取后继续组织最终回答。

## 🖥 WebUI 账户设置

账户设置页随适配器一起发布：`web/` 是源码，构建产物是根目录的 `index.umd.js`（`foxran.yaml` 中的 `adapter.frontend_bundle`）。WebUI 启动时加载它，页面通过 `settingsPanels` 出现在“设置 → 适配器”里，并复用宿主提供的 `window.Foxran.api` 与 `window.Foxran.ui` 组件。需要宿主 4.0.0 及以上。

修改 `web/` 后重新构建：

```bash
cd web && npm install && npm run build
```
