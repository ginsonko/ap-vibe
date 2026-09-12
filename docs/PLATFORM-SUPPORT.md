# Windows、macOS 与 Linux

AP-Vibe 的工作台在本机浏览器打开。不同系统共享会话目录、项目档案、按需恢复、逻辑观察及工作室协议；能执行哪些伙伴任务，取决于该机器安装的客户端和用户配置的模型连接。

| 能力 | Windows | macOS / Linux |
|---|---|---|
| 工作台、项目档案、上下文与逻辑观察 | 支持 | 提供兼容入口，按系统测试记录核对 |
| Codex、Claude、OpenCode、OpenClaw 等接入 | 已安装客户端按支持表接入 | 发现该系统的已安装客户端后接入；缺一个客户端不影响其它能力 |
| 保存 API Key | 当前用户 DPAPI | AES-GCM 加密；本安装的主密钥文件仅当前系统用户可读 |
| 启动、重复启动、端口回退、停止 | PowerShell 生命周期 | Python POSIX 生命周期 |
| Windows 桌面快捷方式 | 支持 | 暂不提供；收藏工作台地址，使用 start.sh 启动 |
| 自动下载更新候选 | 支持 | 支持检查与准备候选；暂不自动切换正在使用的安装 |
| 任意第三方桌面客户端完整托管 | 以逐应用支持表为准 | 同样以实际适配深度为准，不保证所有桌面程序可托管 |

## macOS / Linux 安装

需要 Python 3.11 或更新版本。推荐让 Codex/Claude 根据本机情况选择 Python。发布包已含前端；直接使用源码时，先在 `apps/studio` 执行 `npm ci` 和 `npm run build`（需要 Node.js）。

在项目根目录执行：

```sh
sh start.sh install
```

首次安装建立专用 Python 虚拟环境，安装必要库，并接入本机已发现的编程客户端。不会安装所有第三方应用，也不会仅因安装或保存伙伴配置而调用付费模型。缺少 Claude Code 时，按 Skill 说明征得用户安装授权后安装；其它基础能力仍可用。

命令完成会输出实际 URL。Linux 没有桌面环境时只输出地址，不尝试打开浏览器；服务默认只监听本机回环，远程 Linux 使用 SSH 本地端口转发后在自己的浏览器访问，不需要将服务直接暴露到公网。

```sh
sh start.sh start --skip-open
sh start.sh status
sh start.sh open
sh start.sh stop
```

重复启动会复用原实例；端口被其它程序占用时选择可用端口并保存实际地址。停止前核对工作室活动和进程身份，忙碌时保留任务。所有命令可加 `--config /实际位置/config.json` 使用指定安装；配置冲突不会悄悄迁移数据。

## 配置与备份

- macOS 默认配置：`~/Library/Application Support/AP-Vibe/config.json`。
- Linux 默认配置：`$XDG_CONFIG_HOME/ap-vibe/config.json`，未配置 XDG 时为 `~/.config/ap-vibe/config.json`。数据默认在 `$XDG_DATA_HOME/ap-vibe` 或 `~/.local/share/ap-vibe`。
- 已有早期 `~/AppData/Local/AP-Vibe/config.json` 的 POSIX 用户仍复用该实例，显式指定 XDG 或 `--config` 时尊重用户位置。
- 备份数据时同时备份本安装配置目录内的 `credentials/master.key`。不要分享这个文件；不要把自己的数据、配置或 Key 打包给其他用户。
- POSIX 文件加密依赖操作系统用户权限，不能抵御已能以同一用户运行的恶意程序。不同系统的 Windows DPAPI 凭据不能直接解密；跨系统迁移档案可使用项目导出/导入，伙伴 Key 在新系统重新填写。

初期不要求所有系统拥有相同桌面软件。请参阅 [跨应用支持表](CROSS-APP-SUPPORT.md)，区分“读会话”“共享档案”“托管执行”“自动唤醒”。看到某个适配器未安装时，可以使用已配置的其它伙伴。

## 验证边界

持续集成分别在 Windows、Ubuntu、macOS 上检查适用的功能；POSIX 测试会真实启动本地服务，检查端口占用、重复启动、收据丢失恢复、带空格与中文路径、加密保存及重启保持。测试不用真实 API Key，不调用模型。

系统 CI 通过也不等于已在每种用户桌面、Linux 发行版、浏览器或第三方客户端版本上全部验收。报告问题时附系统、客户端版本、实际入口、出现问题的步骤及已脱敏日志；不要附 Key。
