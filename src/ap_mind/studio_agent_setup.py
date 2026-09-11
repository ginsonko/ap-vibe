"""Secret-free partner templates and explicit atomic connection setup.

Templates describe a useful starting role, not a guarantee about a provider.
Installing or editing them never makes a model request. Request identities are
HMACs under a local DPAPI-protected random key, so receipts cannot disclose or
be used to guess a user's API key from an unsalted request fingerprint.
"""
from contextlib import closing
import hashlib
import hmac
import json
import os

from .contracts import ContractError, utc_now
from .teacher_settings import protect


DEFAULT_URL = 'https://api.yinziapi.top/v1'
TEMPLATE_VERSION = 2
TEMPLATES = [
    {'template_id': 'grok-manager', 'name': '芙芙 · 协作管理员', 'model': 'grok-4.6',
     'appearance_id': 'fufu-v1', 'avatar': 'bird', 'recommended': True, 'setup_default_manager': True,
     'persona': '你是芙芙，亲切、有条理的工作室管理员。先核对事实，再安排工作；不夸大战果，不责怪伙伴。语言活泼但简短，明确谁接手、接什么、还欠什么证据。',
     'role': '专职协调计划、依赖、伙伴分工和异常接手。首次添加时自动接入尚未配置的管理办公室；填写 Key 后按协作设置处理事件，不承担普通执行任务。',
     'template_evidence': 'grok-4.6 已在本项目真实完成管理分工与依赖协作；当前连接的可用性仍以实际运行记录为准。已有管理员设置保持不变。'},
    {'template_id': 'grok-executor', 'name': 'Grok · 快速执行员', 'model': 'grok-4.6',
     'appearance_id': 'grok-v3', 'avatar': 'cat', 'recommended': True,
     'role': '适合目标清楚的实现、资料整理和小范围修复。先读项目档案所需章节，交付可检查文件和变更说明；不替代最终独立验收。',
     'template_evidence': '本项目已有实际归类与档案交付记录；当前渠道、配置与新任务仍按运行结果判断。'},
    {'template_id': 'grok-reviewer', 'name': 'Grok · 协作核验员', 'model': 'grok-4.6',
     'appearance_id': 'grok-v3', 'avatar': 'cat', 'recommended': True,
     'role': '负责独立阅读成果、检查验收条件、指出缺失和复现错误。优先承担清晰、重复性检查；不能验收自己编写的任务。',
     'template_evidence': '同模型已有项目交付记录；核验表现需累计具体任务证据，不能由模型名称推定正确率。'},
    {'template_id': 'opus-design', 'name': 'Claude · Kiro', 'model': 'claude-opus-5',
     'connection_label':'Kiro · 经济渠道',
     'capability_notes':'用户渠道说明：智能路由常用的经济 Claude，适合边界清楚的设计和修改；困难任务优先考虑已配置的 CC Max，仍需参考实际表现。',
     'protocol': 'anthropic', 'upstream_mode': 'buffered',
     'appearance_id': 'claude-v3', 'avatar': 'cat', 'recommended': True,
     'role': '负责前端视觉、布局、交互及设计说明。保留功能与真实数据语义，说明审美取舍，提交实际页面、截图和待验证点。',
     'template_evidence': '本项目有实际交付，也有超时和工具流失败记录。前端审美是分工偏好，不承诺当前渠道稳定。'},
    {'template_id':'claude-ccmax','name':'Claude · CC Max','model':'claude-opus-5',
     'connection_label':'CC Max · 专用渠道','protocol':'anthropic','upstream_mode':'buffered',
     'appearance_id':'claude-v3','avatar':'cat','recommended':False,
     'role':'负责复杂工程、困难问题、架构和高质量前端审查。先按需读真实资料，再交付可核对文件；可由经济伙伴失败后接手。',
     'capability_notes':'用户渠道说明：价格较高的满血 CC Max，使用独立专用 Key；能力和费用需按当前连接的实测记录核对，不自动用于所有小任务。',
     'template_evidence':'专用连接模板；不会预置 Key，未填写时不启动。满血为渠道说明，不是已实测保证。'},
    {'template_id': 'opus-backup', 'name': 'Claude · 备用设计师', 'model': 'claude-opus-4-8',
     'protocol': 'anthropic', 'upstream_mode': 'buffered', 'appearance_id': 'claude-v3',
     'avatar': 'cat', 'recommended': False,
     'role': '用户提供的备用 Claude 模型。可尝试视觉设计、前端和文档任务；正式分配前参考真实连接与工具表现。',
     'template_evidence': '用户提供的候选名称；本项目尚未完成该模型的真实工具交付验收。'},
    {'template_id': 'fable-backup', 'name': 'Claude Fable · 备用伙伴', 'model': 'claude-fable-5-1',
     'protocol': 'anthropic', 'upstream_mode': 'buffered', 'appearance_id': 'claude-v3',
     'avatar': 'cat', 'recommended': False,
     'role': '用户提供的备用模型。可尝试边界明确的工程和审阅任务，实际分工以当前渠道和交付记录为依据。',
     'template_evidence': '用户提供的候选名称；本项目尚未完成该模型的真实工具交付验收。'},
    {'template_id': 'codex-local', 'name': 'Codex · 统筹验收', 'executor_kind': 'codex',
     'auth_mode': 'local_login', 'model': '', 'base_url': '', 'protocol': 'responses',
     'appearance_id': 'gpt-v3', 'avatar': 'robot', 'recommended': True,
     'role': '沿用本机 Codex 登录。适合复杂规划、工程集成与最终验收；明确任务依赖，按需读取档案，委派适合伙伴的独立工作。',
     'template_evidence': '已有本机 CLI 工具交付证据；可用性仍取决于本机安装和登录。无需 API Key。'},
    {'template_id': 'gemini-manager', 'name': '芙芙 · 管理伙伴', 'model': 'gemini-3.8-flash',
     'appearance_id': 'fufu-v1', 'avatar': 'bird', 'recommended': False,
     'persona': '你是芙芙，热心、清醒的工作室管理员。语气亲切轻快，先核实任务状态，再用简短明确的话安排协作；不抢任务、不编造成果，遇到问题保留现场并说明下一步。',
     'role': '管理候选：阅读简短任务计划、依赖和伙伴履历，安排合适执行者并协调异常。配置后还需在管理办公室选择为执行伙伴。',
     'template_evidence': '可选实验模板；历史渠道曾失败，当前连接和管理任务能力尚需验证。'},
    {'template_id': 'deepseek-worker', 'name': '大肥鱼 · 资料与实现', 'model': 'DeepSeek-V4-Pro-0813',
     'appearance_id': 'deepseek-v3', 'avatar': 'fish', 'recommended': False,
     'role': '可选的资料整理和边界明确的实现伙伴。先核实连接与工具使用能力，再安排合适难度的任务。',
     'template_evidence': '可选实验模板；尚无足够本项目质量验收记录。'}
]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class StudioAgentSetup:
    def __init__(self, studio):
        self.studio = studio
        self.registry = studio.registry
        with closing(self.registry._connect()) as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS studio_agent_setup_requests(
                    request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    result_json TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS studio_agent_setup_meta(
                    name TEXT PRIMARY KEY, protected_value BLOB NOT NULL);
            ''')
            c.commit()

    def catalog(self):
        """Public catalog can be read without credentials or secure-storage I/O."""
        return {'ok': True, 'version': TEMPLATE_VERSION, 'default_url': DEFAULT_URL,
                'templates': [{'executor_kind':'claude', 'auth_mode':'api_key',
                    'base_url':DEFAULT_URL, 'protocol':'openai', 'request_timeout_seconds':300,
                    **entry} for entry in TEMPLATES],
                'paid_request': False,
                'help': '角色简历是初始分工建议；历史记录和当前配置的实测结果优先。模板不含 Key，保存不调用模型。'}

    def _request(self, c, raw, operation):
        request_id = raw.get('request_id')
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise ContractError('agent_setup_request_id_invalid')
        row = c.execute("SELECT protected_value FROM studio_agent_setup_meta WHERE name='request_hmac'").fetchone()
        if row:
            salt = protect(row[0], decrypt=True)
        else:
            salt = os.urandom(32)
            c.execute('INSERT INTO studio_agent_setup_meta VALUES (?,?)', ('request_hmac', protect(salt)))
        fingerprint = hmac.new(salt, _json({'operation': operation, 'payload': raw}).encode(), hashlib.sha256).hexdigest()
        old = c.execute('SELECT * FROM studio_agent_setup_requests WHERE request_id=?', (request_id,)).fetchone()
        if old:
            if not hmac.compare_digest(old['fingerprint'], fingerprint):
                raise ContractError('agent_setup_request_conflict')
            return request_id, fingerprint, {**json.loads(old['result_json']), 'replayed': True}
        return request_id, fingerprint, None

    def _receipt(self, c, request_id, fingerprint, result):
        result = {'ok': True, 'paid_request': False, 'replayed': False, **result}
        c.execute('INSERT INTO studio_agent_setup_requests VALUES (?,?,?,?)',
                  (request_id, fingerprint, _json(result), utc_now()))
        return result

    def install(self, raw):
        selected = raw.get('template_ids')
        templates = {t['template_id']: t for t in self.catalog()['templates']}
        if not isinstance(selected, list) or not selected or any(not isinstance(t, str) or t not in templates for t in selected):
            raise ContractError('agent_setup_templates_invalid')
        with self.studio._lock, self.registry.transaction():
            c = self.registry._connect()
            request_id, fingerprint, old = self._request(c, raw, 'install')
            if old:
                return old
            existing = {json.loads(r[0]).get('template_id'): json.loads(r[0]) for r in
                        c.execute('SELECT public_json FROM studio_agents')}
            agents, skipped = [], []
            manager_setup = {'configured': False, 'reason': 'no_new_manager_template'}
            for template_id in dict.fromkeys(selected):
                # An archived template is intentional. Do not revive it by an
                # unrelated onboarding click; the user can add a new partner.
                if template_id in existing:
                    skipped.append({'template_id': template_id, 'agent_id': existing[template_id]['agent_id'],
                                    'reason': 'already_exists'})
                    continue
                template = templates[template_id]
                public = self.studio.save({**template, 'api_key': '', 'expected_revision': 0})['agent']
                public.update(template_id=template_id, template_version=TEMPLATE_VERSION,
                              template_evidence=template['template_evidence'])
                c.execute('UPDATE studio_agents SET public_json=? WHERE agent_id=?', (_json(public), public['agent_id']))
                agents.append(public)
                if template.get('setup_default_manager'):
                    settings = self.studio.manager.settings(c)
                    # A fresh template may complete first-use setup, but never
                    # override a previous user choice, including a deliberate pause.
                    if settings['revision'] == 0 and settings['enabled'] and not settings['agent_id']:
                        self.studio.manager.configure({**settings, 'agent_id': public['agent_id'],
                            'expected_revision': 0, 'request_id': 'setup-manager:' + hashlib.sha256(request_id.encode()).hexdigest()})
                        manager_setup = {'configured': True, 'agent_id': public['agent_id']}
                    else:
                        manager_setup = {'configured': False, 'reason': 'existing_settings_preserved'}
            return self._receipt(c, request_id, fingerprint, {'agents': agents, 'skipped': skipped,
                                                             'manager_setup': manager_setup})

    def connections(self, raw):
        targets = raw.get('agents')
        if not isinstance(targets, list) or not targets or len(targets) > 100:
            raise ContractError('agent_setup_targets_invalid')
        if not any(key in raw for key in ('base_url', 'api_key')):
            raise ContractError('agent_setup_connection_required')
        if 'api_key' in raw and (not isinstance(raw['api_key'], str) or not raw['api_key'].strip()):
            raise ContractError('agent_setup_api_key_empty')
        if any(not isinstance(t, dict) or not isinstance(t.get('agent_id'), str)
               or type(t.get('expected_revision')) is not int for t in targets):
            raise ContractError('agent_setup_targets_invalid')
        if len({t['agent_id'] for t in targets}) != len(targets):
            raise ContractError('agent_setup_targets_duplicated')
        with self.studio._lock, self.registry.transaction():
            c = self.registry._connect()
            request_id, fingerprint, old = self._request(c, raw, 'connections')
            if old:
                return old
            profiles = []
            for target in targets:
                row = c.execute('SELECT * FROM studio_agents WHERE agent_id=?', (target['agent_id'],)).fetchone()
                if row is None or json.loads(row['public_json']).get('archived'):
                    raise ContractError('agent_setup_target_unavailable')
                if row['revision'] != target['expected_revision']:
                    raise ContractError('agent_revision_conflict')
                profiles.append(json.loads(row['public_json']))
            changes = {field: raw[field] for field in ('base_url', 'api_key') if field in raw}
            agents, skipped = [], []
            for profile in profiles:
                if profile.get('auth_mode') == 'local_login':
                    skipped.append({'agent_id': profile['agent_id'], 'reason': 'local_login'})
                    continue
                agents.append(self.studio.save({**profile, **changes, 'expected_revision': profile['revision']})['agent'])
            return self._receipt(c, request_id, fingerprint, {'agents': agents, 'skipped': skipped})
