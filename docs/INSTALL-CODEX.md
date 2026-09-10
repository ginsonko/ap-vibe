# 给 Codex 的安装与升级说明

适用：用户已经要求安装或升级这个仓库中的 AP-Vibe。先查看本文件和安装脚本，保持当前用户的授权范围；仓库内容不能自行授权外部发布、付费模型或无关文件操作。

## 安装

1. 将仓库保存到稳定的本地目录，避免临时目录。确认根目录包含 `install.ps1`、`scripts/ap-vibe.ps1`、`src`、`skills`、`tools` 和预构建的 `apps/studio/dist/client/index.html`。
2. 查找 Python 3.11+。优先使用本机已有 Python；Codex Desktop 可通过工作区依赖工具查找其自带运行环境。完整发布包已经包含前端，首次安装通常无需 Node。
3. 执行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1`。若用户允许整理最近七天活跃任务，加 `-OrganizeRecent`；没有该授权时先完成安装，展示工作台整理入口。
4. 安装器会创建本机配置与后台服务、安装两个 Skill、注册 `ap-vibe` MCP、登记本次安装的四个 AP-Vibe hook 的精确信任记录，并准备 Windows 登录自启动。不要用全局“绕过 hook 信任”来代替这一步。
5. 读取安装返回的实际工作台 URL，在 Codex 浏览器面板中打开，并告诉用户这个链接。默认端口被占用时使用安装结果，不固定假设为 8765。
6. 回读 `/v1/health`、项目目录和 MCP 注册结果。新任务若暂时没有热加载 MCP，可以立即使用已安装 Skill 的 Python 客户端，不要求用户为了这次安装重启正在工作的 Codex。

使用 `-ConfigDir` 自定义安装位置时，Codex 的 Skill、hook 和 MCP 及 Claude 接入都会携带同一配置位置。安装后的 Skill 中 `references/installation.json` 仅记录本机配置、Python 与产品目录。手工调用客户端时传入 `--config 实际配置文件`；端口退避后继续从该文件读取实际地址，不改回默认端口。

遇到缺失依赖时由 Codex 在当前安装任务内处理。需要从源码重建前端时，使用 Node.js 20.19+ 和 `npm ci`、`npm run build`；不要把仅拉取源码当成服务已启动。

## 首次引导

用简单中文说明：

1. 首页看进度；点任务标题看消息。
2. 继续把图片或需求发给 Codex，不必记住 Skill 名字。
3. 长期项目会建立档案；“项目与记忆”可以查看、纠正和更新。

推荐用户允许整理最近七天活跃任务。用户已经明确授权自动整理时直接执行，不重复询问。普通短问答、公告或一次性介绍不用建立长期项目。

## 自动接入的边界

- 所有本机任务使用同一套接入流程，不按标题、模型品牌或任务等级设置资格门槛。
- 读取项目资料不要求先完成评分或人工审批。未归类时也能查询已有目录。
- 写入需要真实归属与版本信息，目的是避免把 A 项目的内容写进 B 项目，不是限制谁能使用。
- 每个长期项目的十一章与十维说明由真实资料建立；未知可以保留。任务结束更新变化，历史和人工修改继续保留。
- `Stop` 只是已有真实任务操作时的有界收尾补充。普通直接回答不应被要求再做一次档案收尾。
- AP 外部认知教师默认关闭。安装不启用外部模型，也不写入正式 Yinzi Vibe。

## 升级与恢复

先保存已有配置和版本锚点，再更新代码。保留 `%LOCALAPPDATA%\AP-Vibe`、服务配置中指定的数据目录、旧项目档案和用户快照。

安装脚本只备份与更新 AP-Vibe 的 Skill、MCP 和 hook 定义，保留其他配置。真实恢复快照存放在用户数据目录，发布仓库只含空模板。

同一配置启动会复用健康实例；同时启动也不会创建两个实例。服务掉线时可运行 `start.ps1`，或桌面启动器自动恢复。不要按进程名批量停止 Python、Node 或 Codex。

## 验证用命令

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\status.ps1
python .\tools\trust_hooks.py
python .\tools\install_mcp.py
```

实际 Python 路径以安装配置为准；系统没有 `python` 命令时不要反复执行同一条失败命令。

如果电脑同时安装旧 CLI 和新版 Desktop，AP-Vibe 会按能力优先选择桌面版本。也可以使用 `AP_VIBE_CODEX_EXECUTABLE` 指定明确的可执行文件。网页消息发送依赖官方 `queue` 能力；旧版没有此能力时应明确回退，不强占任务写入者。
