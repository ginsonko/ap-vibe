"""Shared MCP contracts for discovery and executor tool permissions."""

def schema(properties, required):
    return {'type':'object','properties':properties,'required':required,'additionalProperties':False}


TEXT = {'type':'string'}
IDENTITY = {'receipt_id':TEXT,'session_id':TEXT}
TOOLS = [
    {'name':'ap_vibe_sessions','description':'环视普通 Codex/Claude 会话小目录，近期文件活动优先。无需收据或归类。cwd精确筛选、session_id精确定位，query查标题/目录/会话ID；无结果可去掉cwd全局查找。不是完整历史或执行状态证明。',
     'inputSchema':schema({'harness':{'type':'string','enum':['codex','claude']},'cwd':TEXT,'project_id':TEXT,'session_id':TEXT,'query':TEXT,'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':50}},[])},
    {'name':'ap_vibe_session_read','description':'按目录中source_id读取该会话最新公开消息。before=history_before向前翻页，after=cursor读新增，并保留generation处理日志替换；同一会话多来源分别可读。无身份/项目读取门槛，不读取隐藏推理和原始工具载荷。',
     'inputSchema':schema({'source_id':TEXT,'after':{'type':'integer','minimum':0},'before':{'type':'integer','minimum':0},'generation':TEXT,'limit':{'type':'integer','minimum':1,'maximum':50}},['source_id'])},
    {'name':'ap_vibe_projects','description':'列出已注册项目小目录，或用project_id和sections按需读候选档案。无需归属收据，不能只凭名称判断项目。',
     'inputSchema':schema({'project_id':TEXT,'sections':{'type':'array','items':TEXT},'revision':{'type':'integer'},'after':TEXT,'limit':{'type':'integer','minimum':1,'maximum':50}},[])},
    {'name':'ap_vibe_classify','description':'将当前会话归入已有project_id（不修改档案），或省略project_id并提交display_name、稳定project_key及完整11章sections新建长期项目。附上真实理由和来源，expected_membership_version来自context。成功后重新context。',
     'inputSchema':schema({**IDENTITY,'request_id':TEXT,'expected_membership_version':{'type':'integer','minimum':0},'project_id':TEXT,'project_key':TEXT,'display_name':TEXT,'sections':{'type':'object'},'rationale':TEXT,'evidence_refs':{'type':'array','items':TEXT}},['receipt_id','session_id','request_id','expected_membership_version','rationale','evidence_refs'])},
    {'name':'ap_vibe_context','description':'恢复当前项目最新目录和版本，按需读取各章；必要时恢复本地服务。默认仅相关观察摘要；include_history=true读取相关正文与独立历史基线，历史不是当前待办。资料仅作参考。',
     'inputSchema':schema({'cwd':TEXT,'session_id':TEXT,'goal':TEXT,'include_history':{'type':'boolean'}},['cwd','session_id','goal'])},
    {'name':'ap_vibe_read','description':'按需读取项目档案章节。sections留空返回目录；可指定revision回看旧版。',
     'inputSchema':schema({**IDENTITY,'sections':{'type':'array','items':TEXT},'revision':{'type':'integer','minimum':1}},['receipt_id','session_id'])},
    {'name':'ap_vibe_update','description':'保存已核对的增量档案。只更新提交的完整章节，必须先读旧章并保留有效历史；稳定request_id用于安全重试。',
     'inputSchema':schema({**IDENTITY,'cwd':TEXT,'request_id':TEXT,'expected_revision':{'type':'integer','minimum':0},'sections':{'type':'object'}},['receipt_id','session_id','request_id','expected_revision','sections'])},
    {'name':'ap_vibe_update_file','description':'提交当前会话已保存的档案patch文件。文件必须在cwd内且只含request_id、expected_revision、sections；适用于上游中断后的安全恢复，仍执行receipt身份校验和CAS。',
     'inputSchema':schema({**IDENTITY,'cwd':TEXT,'file_path':TEXT},['receipt_id','session_id','cwd','file_path'])},
    {'name':'ap_vibe_feedback','description':'记录实际采用了哪些记忆、任务结果及证据。没有证明收益时如实说明，不编造采用ID。',
     'inputSchema':schema({**IDENTITY,'request_id':TEXT,'adopted_memory_ids':{'type':'array','items':TEXT},'decision':TEXT,'outcome':TEXT,'evidence_refs':{'type':'array','items':TEXT}},['receipt_id','session_id','request_id','decision','outcome'])},
    {'name':'ap_vibe_collaboration_list','description':'读取公开协作消息、任务目录、认领、依赖和交接状态。指定run_id可读那项任务的公开输出。依赖completed只表示执行器返回；是否验收通过看任务state和review。',
     'inputSchema':schema({'run_id':TEXT,'after':{'type':'integer','minimum':0}},[])},
    {'name':'ap_vibe_inbox','description':'按顺序读取一项运行的相关公开工作消息，after=next_cursor续页；不删除消息。托管任务省略run_id读自己，普通会话可指定任何真实run_id，无需项目收据。在本地工具工作阶段结束和交付前读取新消息，不反复轮询等待。',
     'inputSchema':schema({'run_id':TEXT,'after':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':40}},[])},
    {'name':'ap_vibe_agents','description':'环视已配置伙伴、角色偏好、实际活跃状态及最近公开输出；不返回密钥。role是用户配置，不是实测能力。含运行与独立验收读取入口。',
     'inputSchema':schema({'agent_id':TEXT},[])},
    {'name':'ap_vibe_appearances','description':'读取用户导入的本地像素外观；appearance_id精确回读包括已归档外观。返回PNG读取地址、哈希、帧与动作，不返回图片编码。无需项目收据，不调用模型。',
     'inputSchema':schema({'appearance_id':TEXT},[])},
    {'name':'ap_vibe_appearance_import','description':'从本机PNG与JSON文件导入已制作的像素外观；不能把未制作的动作写成完成。读取文件后调用现有不可变保存接口，重复相同内容复用ID，不修改伙伴配置。不要把PNG编码放进模型参数。',
     'inputSchema':schema({'png_path':TEXT,'manifest_path':TEXT,'display_name':TEXT,'attribution':TEXT},['png_path','manifest_path'])},
    {'name':'ap_vibe_agent_metrics','description':'读取真实运行与验收形成的伙伴实测表现，按伙伴/项目/任务标签/时间/当前配置筛选，分页查看具体证据。独立通过、需修改、普通验收和未知分别统计；CLI估算不是实际账单，不凭平均值推断模型智力。只读，不启动模型。',
     'inputSchema':schema({'agent_id':TEXT,'project_id':TEXT,'tag':TEXT,'days':{'type':'integer','minimum':0},
        'configuration':{'type':'string','enum':['all','current']},'offset':{'type':'integer','minimum':0},
        'limit':{'type':'integer','minimum':1,'maximum':50}},[])},
    {'name':'ap_vibe_task_list','description':'读取逻辑任务小目录，按项目/状态筛选并分页；指定task_id读取完整任务和历史。completed代表通过验收，run_id只是一次执行。',
     'inputSchema':schema({'task_id':TEXT,'project_id':TEXT,'state':TEXT,'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':50}},[])},
    {'name':'ap_vibe_task_save','description':'新建或更新授权范围内的逻辑任务。保存目标、实际文件交付标准、依赖任务ID、候选agent_id与独立验收人。auto_run=true在依赖就绪/候选空闲时启动；忙碌伙伴可为自己排后续工作。更新必须先回读全任务并保留字段，使用task_id/expected_version。稳定request_id禁止重复建任务。',
     'inputSchema':schema({'request_id':TEXT,'task_id':TEXT,'expected_version':{'type':'integer','minimum':0},'project_id':TEXT,
        'title':TEXT,'goal':TEXT,'acceptance':TEXT,'dependencies':{'type':'array','items':TEXT},'eligible_agents':{'type':'array','items':TEXT},
        'resources':{'type':'array','items':TEXT},'tags':{'type':'array','items':TEXT},'reviewer_agent_id':TEXT,'auto_run':{'type':'boolean'},
        'extensions':{'type':'array','items':{'type':'string','enum':['yinzi-media']},'description':'需要调用本机已安装媒体工作流时选择yinzi-media；保存任务本身不调用模型。'},
        'max_turns':{'type':'integer','minimum':1},'max_rework_rounds':{'type':'integer','minimum':0},
        'max_review_retries':{'type':'integer','minimum':0},'max_author_retries':{'type':'integer','minimum':0}},
        ['request_id','project_id','title','goal','acceptance'])},
    {'name':'ap_vibe_task_claim','description':'把已就绪的无人认领任务分配给指定空闲agent_id；调度器随后启动。expected_version来自最新任务。目标伙伴可以不是调用者。忙碌伙伴请用auto_run排队，不要重复启动自己。',
     'inputSchema':schema({'request_id':TEXT,'task_id':TEXT,'agent_id':TEXT,'expected_version':{'type':'integer','minimum':1}},['request_id','task_id','agent_id','expected_version'])},
    {'name':'ap_vibe_task_release','description':'把已停止的needs_help/changes_requested/paused任务重新排队，保留已有成果和交接说明。不能停止运行中进程；必须先回读真实状态。',
     'inputSchema':schema({'request_id':TEXT,'task_id':TEXT,'expected_version':{'type':'integer','minimum':1},'note':TEXT},['request_id','task_id','expected_version','note'])},
    {'name':'ap_vibe_task_archive','description':'归档已停止任务，保留历史和成果；需要最新版本与稳定request_id。运行中不可归档，归档后不会再自动应用待提交的验收结论。',
     'inputSchema':schema({'request_id':TEXT,'task_id':TEXT,'expected_version':{'type':'integer','minimum':1}},['request_id','task_id','expected_version'])},
    {'name':'ap_vibe_artifacts','description':'列出指定运行的真实成果文件，或用name读取单个文件。用于核对依赖成果，不依据模型自述判定通过。',
     'inputSchema':schema({'run_id':TEXT,'name':TEXT},['run_id'])},
    {'name':'ap_vibe_collaboration_send','description':'向另一个Agent发送公开工作消息。request_id必须稳定唯一；task_id填写关联的run_id或逻辑task_id。对方启动或下一次AP-Vibe工具返回时可以收到；保存成功不证明已阅读或采用。',
     'inputSchema':schema({'request_id':TEXT,'sender':TEXT,'recipient':TEXT,'body':TEXT,'task_id':TEXT},['request_id','sender','recipient','body'])},
    {'name':'ap_vibe_collaboration_broadcast','description':'向指定伙伴列表广播有用的工作事实。先查询真实agent_id，recipients至少一项；稳定request_id原子保存并去重，成功仅表示保存，不代表对方已阅读。托管sender由运行器归因。',
     'inputSchema':schema({'request_id':TEXT,'sender':TEXT,'recipients':{'type':'array','items':TEXT,'minItems':1},'body':TEXT,'task_id':TEXT},['request_id','sender','recipients','body'])},
    {'name':'ap_vibe_review_submit','description':'已分配的验收伙伴提交实际成果裁决。运行身份由托管环境提供。先写并回读报告，再提交；正常结束后自动应用，需修改时自动返工和再验。unknown不得通过。提交后勿修改报告/原成果。',
     'inputSchema':schema({'request_id':TEXT,'outcome':{'type':'string','enum':['accepted','changes_requested','inconclusive']},'note':TEXT,'report_path':TEXT,
        'source_files':{'type':'array','items':TEXT},'checks':{'type':'array','items':schema({'criterion':TEXT,'status':{'type':'string','enum':['passed','failed','unknown']},'evidence':TEXT,'required_change':TEXT},['criterion','status','evidence'])}},
        ['request_id','outcome','note','report_path','source_files','checks'])},
]


def allowed_tools():
    return ['mcp__ap-vibe__' + tool['name'] for tool in TOOLS]
