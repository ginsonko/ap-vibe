# Grok 接入设计

Grok Desktop / CLI 作为独立应用加入现有 AP-Vibe 会话、档案、工作室和消息链路；通过 Claude Code 使用 Grok 模型的伙伴继续保留。

## 模块与配置

- `grok_sessions.py` 读取桌面公开消息和 CLI ACP 日志，区分桌面 ID 与底层原生 ID；隐藏推理与原始工具参数不进入公开记录。
- `grok_runner.py` 处理原生命令、流式输出和真实完成标志；`native_runner.py` 复用已有工作空间、成果、依赖、停止与独立验收。
- `grok_transport.py` 在每次运行内处理 Chat Completions 空流帧兼容、真实用量与预算；不重放请求。
- `grok_messages.py` 使用原应用本机消息 API。先保存待发记录，忙碌时等待，空闲再投递；未发可撤回，不确定发送不自动重放。
- `install_grok.py` 增量安装两套 home 的 Skill/MCP/规则/Hook；`grok_context_hook.py` 只登记被动生命周期。用户修改保留并报告。
- 注册、来源开关、角色、人设、协议与模型选择均接入通用功能，避免独立的第二套工作室。

## 发布审查

从最新远端源码创建隔离候选，保留历史功能与第三方接入。发布包包含模块、前端与安装入口；升级沿同一配置和数据目录，忙碌时暂缓切换。用有界协议测试、隔离安装升级、真实客户端小样与公开下载回读区分各层证据。

实际支持与操作步骤见 [接入指南](GROK-INTEGRATION-ACCEPTANCE-20260914.md)。
