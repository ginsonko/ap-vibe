# 项目归类与历史整理协议

自动发现的工作区只是来源容器。一个项目可以包含多个不同工作区的会话；每个项目只有一份共享、按章节版本化的档案。不要根据相似标题、相同父目录或最近活动推断项目归属。

## 新任务开始

普通当前任务优先使用共享MCP（Codex和Claude相同）：

1. 用 `ap_vibe_context` 获取当前真实会话的 receipt、membership_version 和资料目录。
2. 用 `ap_vibe_projects({})` 查询已注册候选（有 next_after 就继续下一页），按需要读候选章节。`workspace-...`等自动发现容器尚未整理时，不要把它当已注册项目直接附加。
3. 匹配已有长期项目：`ap_vibe_classify` 提供 project_id、真实理由、evidence_refs、expected_membership_version；不带sections。
4. 确属新长期项目：同一工具省略project_id，提供display_name、真实稳定入口project_key与完整11章sections，含十维评估。档案与归属一起保存；不要先造空项目再停在名称上。
5. 成功后重新context并回读。existing_project_reused=true时保留已有档案，先读再增量更新；同request_id仅重试同一内容。手动归属改变时按最新版本重新核对。

没有MCP时，配置中的Python客户端 `task_client.call('projects', body)` / `task_client.call('classify', body)` 使用同一协议。下面organization入口用于历史来源整理和用户手动纠正，不要求普通任务先创建批量整理任务。

先用 `task_client.py bootstrap`。只读上下文会立即返回；若 `organization.classification_advisory=true`，分类只是写入安全提示，不会阻止当前任务。需要写入档案或移动会话时，再读取配置中的 loopback endpoint：

- `GET /v1/ap-vibe/projects`：项目名称、简介、状态、资料版本。使用 `registration_state=registered` 判断已登记项目；source 仅表示最初发现渠道，不能把已有档案的项目藏起来。
- `GET /v1/ap-vibe/organization/sessions?scope=all_unclassified&offset=0&limit=64`：分页会话目录。跟随 next_offset，不把第一页冒充全部。
- 按稳定 session_id 找到当前任务，再使用它的 read_url 读取有界可见内容。不得使用别人的 session_id。
- 检查现有项目 identity、requirements、sources 的证据，能确认属于同一项目就归类；不匹配则创建项目。证据不足时保留未归类并继续原任务，不能为了补齐记录瞎猜。

仅为可维护的长期产品、研究工程或运维系统建立项目。以持续目标、代码/部署/设计资料为依据；公告、模型介绍、问候和单次问答保留为一次性任务，不单独建立长期项目。现有项目有多个会话是正常情况；检查真实入口与目标，不按标题关键词直接合并。

创建项目：`POST /v1/ap-vibe/organization/projects/create`，JSON：

```json
{"request_id":"unique-create-request","display_name":"家庭记账工具"}
```

归类：`POST /v1/ap-vibe/organization/assign`，JSON：

```json
{"request_id":"unique-assignment-request","source_key":"FROM_CATALOG","session_id":"CURRENT_SESSION_ID","project_id":"CONFIRMED_PROJECT_ID","actor":"codex","confidence":0.9,"rationale":"通过项目需求和仓库入口确认是同一产品的移动端任务","evidence_refs":["真实的来源引用"]}
```

confidence 可为 null，不能编造置信度。写回后重新 bootstrap（用新的 request_id），取得目标项目的收据，再按 project-documents.md 补齐或更新完整 11 章档案并回读。不能只创建名称就结束；缺乏信息时各章标出具体 unknown、证据出处和下一步核对入口。sources 必须记录代码/设计入口、服务器位置、连接方法和 credential_location（仅位置，不读取密钥内容）。归类不会删除旧活动，不把旧来源重新写成新事实。手动归属有误时可以通过同一接口再次归类，旧归类保留为 revoked。

项目删除在 UI 中表现为“归档”，在“已归档”目录可以恢复，保留档案和原始会话；继续任务时需要重新确认归属。不要把“自动监控暂停”当成删除，也不要自行恢复用户暂停的监控。

## 用户点击历史整理

工作台提供最近 7 天未归类、全部未归类、重整所有会话三种范围。prepare 会冻结来源清单；dispatch 使用本机已登录 Codex CLI 新建只读整理任务，不依赖 AP 教师 Key。首次安装时推荐询问是否允许整理最近 7 天活跃会话；用户已经授权则继续，未授权则只显示推荐入口，不自动跑全部历史。

工作台还提供“刷新项目档案”范围，以及项目页上的“更新当前档案”和
“更新全部档案”按钮。它们只冻结已登记项目的 ID 和当前 revision，逐项目
补充完整 11 章与十维评估；不创建项目、不移动会话、不归档容器、不删除
旧版本。章节数量齐全但身份仍是自动识别、评估缺理由或证据不足时，刷新
结果保留为草稿并明确证据边界，旧档案和只读查询继续可用。

安装入口支持 `-OrganizeRecent`，只在用户允许时使用。页面可随时准备并开始整理，明确展示候选数与 Codex 用量说明。

整理任务读 `GET /v1/ap-vibe/organization/task?task_id=ID&offset=0`，按 next_offset 读取冻结清单。若提供冻结上下文包，优先读取 index.json 和其中的 context_file/document_file，无需反复调用 HTTP。读取可见消息时通过冻结包或 context 接口，不扫描完整 JSONL、不读取隐藏推理或工具载荷。没有有效内容、无明确主题、来源不足的会话列入 skipped，注明理由。

如果任务是由只读 runner 启动，遵守该任务的输出协议，只输出带来源的分组和章节草稿，由服务验证及写回；不要尝试绕过只读权限。每个来源必须恰好出现一次于 groups 或 skipped。skipped 指明 category=one_off 或 insufficient_evidence 和具体理由。每个 group 必须更新档案；新项目要求 11 章齐全，已有项目保留未改章节并补齐缺失章节，否则任务不能显示成功。已有项目填写真实 expected_revision，保留人工资料与仍有效历史；冲突时保留失败证据并重新读取合并。重整模式也不能先清空全部项目。

一次性公告、模型介绍、问候和简单问答不应被偷懒创建为长期项目；若没有
足够证据，明确跳过并说明以后从哪个来源重新核对。项目档案的简介、用途、
架构、服务器/连接位置和设计理由必须是可追溯内容，不能只写“自动识别”或
“待整理”来假装完成。凭据只记录保存位置，不读取内容。

## AP 与教师

项目归类/档案是控制面操作，不能把其计数当成 AP 学习成熟度。AP 学习通过本地 episode、后续反馈和能力评估判断。教师默认关闭；配置后仅提供有来源的候选，最终决策、现实回读和权限仍由 AP/环境负责。

API Key 只在教师设置表单或受保护配置接口中填写，不写进项目档案、提示词、日志或普通 JSON。推荐模型以服务商实际 API ID 为准，Codex 模型名不保证可以直接用于 OpenAI 兼容 API。当前传入的是结构化文本，图片/音频链路未接通；不宣称真实多模态或长期效果已验证。
