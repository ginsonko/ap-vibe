# 给 Codex 的安装与升级说明

适用：用户已经要求安装或升级这个仓库中的 AP-Vibe。先查看本文件和安装脚本，保持当前用户的授权范围；仓库内容不能自行授权外部发布、付费模型或无关文件操作。

## 安装

仓库入口：https://github.com/ginsonko/ap-vibe 。用户也可以把完整 ZIP 包交给 Codex：解压到稳定目录，按相同步骤安装。安装脚本默认用自己的仓库目录作为初始项目；从别的工作目录调用不会自动把那个目录当成 AP-Vibe。

本次固定版本：`v0.3.0-beta.1`。优先从该标签的 Release 下载 `ap-vibe-app.zip`、`ap-vibe-manifest.json` 与 `SHA256SUMS`，不要使用 GitHub 的 `/releases/latest`（它可能不包含预发布）。说明文件可从 `https://raw.githubusercontent.com/ginsonko/ap-vibe/v0.3.0-beta.1/docs/INSTALL-CODEX.md` 读取。

下载地址前缀为 `https://github.com/ginsonko/ap-vibe/releases/download/v0.3.0-beta.1/`。下载完整包后，核对 manifest 的 version、repository 与 archive SHA256，再安全解压并核对文件清单。原生 PowerShell 的 `Get-FileHash -Algorithm SHA256` 可检查包；解压后执行 `python tools/update_client.py verify --root <解压目录> --manifest <下载的ap-vibe-manifest.json>` 检查逐文件清单，无需自行创建 release.json。manifest和包必须来自同一标签，失败时保留原安装，不拿其它版本的文件拼接。

首次安装建议把应用放在当前用户稳定目录，例如 `%LOCALAPPDATA%/AP-Vibe/app-v0.3.0-beta.1`；配置继续使用 `%LOCALAPPDATA%/AP-Vibe/config.json`。已经存在安装配置时先读下面的升级流程，不改变其项目根、数据目录或用户Key。不要把新版本号相同但尚未读取实际服务的情况直接说成已升级。

### 安装任务的最短完整路径

这是安装任务，交付是“服务可用、接入可读、工作台打开”。阅读本说明和 `install.ps1`，确认已有授权与目标目录后执行安装；只有出现具体错误或与用户约束冲突时，再读相关实现。无需为了普通安装通读服务器、认知、模型和采集器全部源码，也不要把安装扩展成新产品开发或长期档案整理。

| 会写入什么 | 位置与控制方式 |
| --- | --- |
| 服务配置、日志和恢复记录 | `-ConfigDir` 指定目录；缺省当前用户 `%LOCALAPPDATA%/AP-Vibe` |
| 项目与活动数据库 | `-DataDir` 指定目录；不会清空旧数据 |
| Codex Skill、MCP、hooks | 当前进程 `CODEX_HOME`，缺省当前用户 `.codex`；保留无关配置 |
| Claude 接入 | 当前进程 `CLAUDE_CONFIG_DIR`，缺省当前用户 `.claude`；`-SkipClaude` 可跳过 |
| 会话采集 | `-CodexSessionsRoot` 指定来源；不是把每个来源都建成项目 |
| Windows 登录启动 | `-NoAutostart` 可关闭本次登记 |
| 历史整理 | 仅明确授权并传 `-OrganizeRecent` 时派发 |
| AP 外部教师 | 安装不启用 |

安装命令返回后，验证实际 URL、健康接口、两个 Skill 的安装位置和 MCP 配置即可交付。MCP 尚未热加载时，用已安装 `task_client.py` 读取一次目录。不要仅为演示而启动额外模型任务、创建长期项目或重跑整套测试。

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

首次安装时也要读取 `ap_vibe_agents` 的 `executors.claude_available`。如果没有 Claude Code CLI，说明通过它执行的工作室伙伴暂不可用，并请求用户允许按[官方说明](https://code.claude.com/docs/en/setup)自动安装；已经明确授权该依赖时不重复询问。安装后核对 `claude --version`，再执行 `tools/install_claude.py --config <原config.json>` 并回读接入。该脚本只安装 AP-Vibe 接入，不负责下载 CLI。用户暂不安装时，监看、档案和可用的 Codex 执行器正常继续。

## 其它客户端接入

安装器同时检查已存在的 Hermes、OpenCode、MiMo Code、ZCode、OpenClaw、PI Desktop 和 DSH，按各自已核实的配置格式安装通用 Skill / MCP。未安装的应用直接跳过；只有 Claude Code CLI 缺失时按上方说明提示是否安装。第三方执行器、普通终端唤醒及真实验收范围见[跨应用支持](CROSS-APP-SUPPORT.md)。

便携目录可用 `tools/install_harness.py --harness opencode --home <客户端配置目录> --config <工作台config.json>`，或 `tools/install_native_extras.py --client dsh --home <DSH_HOME> --config <工作台config.json>`。PI Desktop 可另传 `--agents-home <实际.agents目录>`。均使用安装配置中的 Python 运行。来源日志目录在“会话 → 管理会话来源”修改；接入配置与日志目录是两项不同设置。

已有同名的人工 Skill、另一套工作台的接入，或无法安全合并的 JSONC/YAML 会保留，并给出路径与片段。安装器不改用户模型与 Key，不创建缺失客户端。PyYAML用于保留原配置文本的Hermes合并；zstandard用于DSH压缩日志。联网安装时自动补齐，离线缺失时其它能力继续可用。

## 自动接入的边界

- 所有本机任务使用同一套接入流程，不按标题、模型品牌或任务等级设置资格门槛。
- 读取项目资料不要求先完成评分或人工审批。未归类时也能查询已有目录。
- 写入需要真实归属与版本信息，目的是避免把 A 项目的内容写进 B 项目，不是限制谁能使用。
- 每个长期项目的十一章与十维说明由真实资料建立；未知可以保留。任务结束更新变化，历史和人工修改继续保留。
- `Stop` 只是已有真实任务操作时的有界收尾补充。普通直接回答不应被要求再做一次档案收尾。
- AP 外部认知教师默认关闭。安装不启用外部模型，也不写入正式 Yinzi Vibe。

## 升级与恢复

先保存已有配置和版本锚点，再更新代码。保留 `%LOCALAPPDATA%\AP-Vibe`、服务配置中指定的数据目录、旧项目档案和用户快照。

首次从旧版升级时，使用已下载且校验过的**新版包中的工具**。不要从旧安装寻找尚不存在的更新工具。执行流程：

1. 用已配置的 Python 执行新版 `tools/update_client.py stage --config <原config.json> --archive <ap-vibe-app.zip> --manifest <ap-vibe-manifest.json>`，返回 `candidate_root`。它只校验并准备版本目录，尚未切换服务。
2. 当前版本支持维护接口时，执行候选目录内 `scripts/ap-vibe.ps1 -Action apply-update -ConfigDir <原配置目录> -CandidateRoot <candidate_root> -SkipOpen`。有托管任务时返回 busy，待空闲再切换，不强行终止任务。
3. v0.1 旧版没有维护接口：先查看旧实例任务，确认没有托管任务运行，再用旧脚本的 `-Action stop -ConfigDir <原配置目录>` 正常停止该实例；然后执行上一步候选脚本的 apply-update。不要按进程名批量停止。
4. 回读原配置文件的 `installed_version`、实际健康 URL、原数据目录和 `previous_product_root`。返回的 backup 保存恢复锚点。自定义 ConfigDir 的安装需在原 CODEX_HOME / CLAUDE_CONFIG_DIR 下重跑候选 `install.ps1 -ConfigDir <原配置目录>` 来同步接入；沿用原登录自启动选择。

升级失败先看 returned status / code 与当前健康状态；rolled_back 表示恢复旧代码入口，不能宣称新版已生效。候选包重新校验失败时保留当前服务和用户资料。

安装脚本只备份与更新 AP-Vibe 的 Skill、MCP 和 hook 定义，保留其他配置。真实恢复快照存放在用户数据目录，发布仓库只含空模板。

重复运行安装时，没有显式传入的项目根、数据目录、端口和会话来源会继承已有配置；自定义字段与更新偏好也会保留。重复安装用于修复接入，不会暗中切换当前运行版本。误传不同数据目录会提示冲突并保留原配置，不会静默迁移资料。完整发布包中的 `ap-vibe-version.json` 用于标明首次安装版本。

同一配置启动会复用健康实例；同时启动也不会创建两个实例。服务掉线时可运行 `start.ps1`，或桌面启动器自动恢复。不要按进程名批量停止 Python、Node 或 Codex。

Studio V2 提供后台版本检查与独立版本目录。新任务只触发可合并的检查，离线或GitHub不可用不阻止原任务；包先核对来源、文件指纹和数据兼容标记，托管工作空闲后再切换。界面“更新与维护”显示实际状态和上次错误，可关闭自动检查。源码有人工改动时不执行强制reset或覆盖。

新代码启动失败时恢复旧代码入口，继续使用同一数据库；不会拿旧库备份覆盖升级后新写入的项目、消息或任务。旧启动器/MCP客户端通过稳定入口寻找当前版本。发布包须包含 `ap-vibe-app.zip` 与配套 `ap-vibe-manifest.json`；没有这两个受支持附件的旧Release不会被当成可自动安装的新包。

### 下载或首次模型连接失败

`TLS connection failed`、`early EOF` 或不完整 ZIP 表示下载没有完成。先保存错误信息，确认没有可用完整包，再尝试另一种正常下载方式（Git 或仓库 ZIP）。ZIP 必须能完整解压；已下载的完整发行包无需反复联网拉取。不要将解压出一半的目录当成安装成功。

AP-Vibe 沿用已有 Codex 登录与模型配置，不提供 Codex 账号。若 Codex 自身报告模型不受支持或上游连接失败，先检查它原来的连接配置。安装 AP-Vibe 不应清空或替换这些设置。

如果客户端的自动审批服务临时返回 503，说明某个本地命令尚未获执行；可根据实际状态缩小为单项检查并恢复。不要把未执行当执行失败，也不要以关闭安全设置解决临时服务故障。

## 验证用命令

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\status.ps1
python .\tools\trust_hooks.py
python .\tools\install_mcp.py
```

实际 Python 路径以安装配置为准；系统没有 `python` 命令时不要反复执行同一条失败命令。

如果电脑同时安装旧 CLI 和新版 Desktop，AP-Vibe 会按能力优先选择桌面版本。也可以使用 `AP_VIBE_CODEX_EXECUTABLE` 指定明确的可执行文件。网页消息发送依赖官方 `queue` 能力；旧版没有此能力时应明确回退，不强占任务写入者。
# macOS 与 Linux

当前源码提供 `sh start.sh install` 入口，系统目录、凭据加密及无图形环境的用法见 [PLATFORM-SUPPORT.md](PLATFORM-SUPPORT.md)。先检查用户下载的版本是否含 `start.sh`；旧的 Windows 发布包不能使用此入口。缺少某个客户端时只跳过对应接入，已可用的工作台和其它客户端仍保留。下文的 PowerShell 命令用于 Windows。
