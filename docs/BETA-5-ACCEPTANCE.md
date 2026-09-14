# v0.4.0-beta.1：Grok 原生接入内测范围

本版在 v0.3.1-beta.1 的任务监看、项目档案、逻辑观察和 Agent 工作室基础上，加入 Grok Desktop / CLI 原生接入。

## 验收结论

- Windows Grok Desktop / CLI 1.0.30：真实公开会话读取、原生会话接续、工作室托管、桌面留言队列均已验收。
- 空闲投递、忙碌等待后投递、待发撤回的公开用户消息次数为 1、1、0；发送不确定时保留原请求，不自动重发。
- Grok 专项与安装升级合同测试 37 项通过；全量 Python 回归 784 项通过、5 项跳过；前端生产构建和 Studio Node 测试通过。
- macOS/Linux 已通过路径、生命周期、安装合同和 CI 覆盖；真实 Grok 客户端验收仍以 Windows 版本为准。

## 安装与升级

从 GitHub Release 下载完整应用包和清单。安装器会发现 `~/.grok` 与桌面 `agent-home`，按配置增量写入 Skill、MCP、规则和 Hook，保留用户的模型、Key、其它 MCP、人工规则、项目档案和消息。若只安装桌面端，仍可接入其 `agent-home`。

升级先校验来源、版本和 SHA-256；有托管任务时暂缓切换。失败自动恢复旧代码入口和同一数据目录，不覆盖升级期间新写入。

## 边界

公开会话只含用户/助手文本和有界工具活动，不展示隐藏思考或原始工具参数。模型协议配置支持 Chat Completions、Anthropic Messages 与 Responses；本版真实 Grok 小样使用 Chat Completions。跨进程“恰好一次”无法由 AP-Vibe 单方面保证，原生回执和不确定状态会保留。该版本是个人非商用 beta。
