# Grok Desktop / CLI 接入指南

Grok 可以和 Codex、Claude Code 及其他已接入客户端共用项目资料、查询公开会话进度，并在 Agent 工作室接收任务。这是 Grok 应用的原生接入；已有通过 Claude Code 运行 Grok 模型的伙伴仍可使用。

## 三步开始

1. 安装或更新 AP-Vibe v0.4.0-beta.1。已安装的 Grok CLI 和桌面 agent-home 会分别获得 Skill、MCP、规则和 Hook；仅桌面端也可发现。保留原模型、Key、其它 MCP 和人工设置。
2. 在「会话」选择 **Grok Desktop / CLI**，查看名称、模型和公开记录。在 Grok 中说「通过 AP-Vibe 读取这个项目的档案和其他会话的最新进展，继续未完成的任务」，使用自然语言接续。
3. 在「Agent 工作室 → 伙伴与配置」添加伙伴，执行端选 **Grok Desktop / CLI**，填写自己的 URL、Key 和模型。可配置人设、外观、擅长任务、费用参考与可选预算。

桌面保持运行时，可在对应会话下方「发送给 Grok」。原窗口忙碌时消息保存在本地，结束当前轮后再发送；待发时可撤回。工作室成果也可沿此队列返回。已有 Grok 会话在自然阶段重连 MCP 或新开会话后加载安装内容。

## 已验证内容

客户端实测：Windows，Grok Desktop / CLI 1.0.30。真实托管 Grok 调用 AP-Vibe MCP 读取指定桌面会话、写出成果，再复用相同原生 ID 和 home 接续。两次成果均由原会话独立回读并正式验收。

真实桌面消息测试：第一条空闲投递并执行工具；第二条在忙碌时本地等待后投递；第三条待发撤回。原窗口中三条用户消息计数分别为 **1、1、0**，前两条有真实回复。最初直接使用原生忙碌队列出现双处理，修复后采用本地等待；不删除原事故记录。

页面实测覆盖应用筛选、桌面阅读、发送/撤回、伙伴执行端与协议选择，1440px 和 390px 浏览。安装合同测试覆盖重复安装、双 home 升级、已有 MCP、用户自定义 Skill/规则/Hook 保留、冲突报告与桌面独立发现。流测试覆盖完成标志、工具活动、用量、隐藏内容过滤与 `choices:null` 空 SSE 帧兼容。

## 能力说明

| 场景 | 支持方式 |
| --- | --- |
| 桌面与 CLI 记录 | 来源目录自动发现，可关闭或自定义；按公开消息及真实原生状态投影 |
| 项目记忆与协作 | 原生 Skill/MCP 查询档案、近期进展、收件箱和有效协作策略 |
| 托管创建与接续 | Grok CLI 独立工作空间，使用真实原生 ID/home，成果进入统一验收 |
| 桌面窗口留言 | 原应用本机 API；本地待发、空闲投递、撤回尚未发送消息 |
| 模型协议 | 可选 Chat Completions、Anthropic Messages、Responses；真实模型小样使用 Chat Completions，其余有配置合同覆盖 |
| 用量与费用 | Chat Completions 读取供应商实际 usage；缺失保持未知，参考金额与实际账单分开 |

普通外部 CLI 在下一次 Skill 调用读取收件箱。桌面新建、停止或已投递撤回接口不列入本版支持；托管 CLI 的创建/停止/接续通过工作室管理。外部用户可能恰在空闲检测与提交之间开启新轮，真实原生回执始终保留；不确定请求不会自动再发。

Grok 路径实现覆盖 Windows/macOS/Linux，真实客户端测试为上述 Windows 版本。若客户端格式变化、配置有人工改动或无法安全合并，界面和安装报告会保留具体原因；不会替换你的模型连接。

## 自定义目录与补装

使用当前安装配置中的 Python 执行：

```text
python tools/install_harness.py --harness grok --config <原AP-Vibe config.json>
```

CLI 默认 `~/.grok`，可设 `GROK_HOME`。桌面默认 Windows `%APPDATA%/grokapp/grok-app/data`、macOS `~/Library/Application Support/grokapp/grok-app/data`、Linux `$XDG_CONFIG_HOME/grokapp/grok-app/data`（未设置时 `~/.config`），可设 `GROK_DESKTOP_DATA_DIR`。便携客户端可用 `--home` 指定单个 agent home。

发布包不含 Grok 软件、登录信息或 API Key。安装与升级的完整步骤见 [安装说明](INSTALL-CODEX.md)。
