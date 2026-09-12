"""Persistent user routing guidance; experience never rewrites user instructions."""
from contextlib import closing
import hashlib
import json

from .contracts import ContractError, utc_now

DEFAULT_GUIDANCE = '''先理解目标、难度、错误代价和模块边界，再选伙伴；不要把所有任务交给最便宜的模型。
理解、架构规划和影响结果的关键决策适合能力强的伙伴。Astra/Codex负责简短关键规划和最终确认；CC Max用于困难攻坚、核心可靠性，普通杂活不默认使用。
规划明确后的实现、批量重复和耗时工作优先适合的经济伙伴。Grok/GLM偏明确编码，Gemini偏文案、社科和讨喜体验，DeepSeek偏推理、数学与拆解，Kimi偏前端。这些是可修改的初期偏好，不是能力保证。
普通测试和验收选择逻辑适配、独立于作者的伙伴，不必使用顶级模型；高难或高影响的最终决定可升级。遇到具体困难先定位问题，避免盲目重试或无差别换高价模型。
设计优先模块化：写清接口、输入输出、依赖、验收与文件写入归属；按独立模块派发，交接必要资料和成果路径。小任务直接完成，避免协调成本超过收益。
结合同类、配置兼容且有独立裁决的真实战绩，逐步修正初始印象；同时查看已知费用、完成时间和返工。网络故障不当作品质量，未知费用不当免费，缓存收益不重复折算。
用户明确要求和当前任务约束优先。用户的指导纲领保持不变，经验以可追溯结果参与推荐，不自动改写个人习惯。'''
DEFAULTS = {'guidance': DEFAULT_GUIDANCE, 'premium_routine_penalty': 0.20,
            'experience_scale': 1.0}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class StudioGuidance:
    def __init__(self, studio):
        self.registry = studio.registry
        with closing(self.registry._connect()) as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS studio_routing_guidance(
                    revision INTEGER PRIMARY KEY,payload_json TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS studio_routing_guidance_requests(
                    request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,result_json TEXT NOT NULL);
            ''')
            c.commit()

    def settings(self, c=None):
        if c is None:
            with closing(self.registry._connect()) as connection:
                return self.settings(connection)
        row = c.execute('SELECT * FROM studio_routing_guidance ORDER BY revision DESC LIMIT 1').fetchone()
        return ({**DEFAULTS, 'revision': 0, 'source': 'default'} if not row else
                {**json.loads(row['payload_json']), 'revision': row['revision'], 'source': 'user'})

    def state(self):
        with closing(self.registry._connect()) as c:
            history = [{'revision': r['revision'], 'created_at': r['created_at']} for r in c.execute(
                'SELECT revision,created_at FROM studio_routing_guidance ORDER BY revision DESC LIMIT 10')]
            return {'ok': True, 'settings': self.settings(c), 'defaults': DEFAULTS,
                    'history': history, 'paid_request': False,
                    'help': '纲领供真实管理员决策；数值偏好也用于管理不可用时的本地排序。真实战绩持续保存，不自动覆盖你的纲领。'}

    def prompt(self):
        settings = self.settings()
        return ('工作台分工指导纲领（' + ('默认参考' if settings['source'] == 'default' else
                f"用户配置第{settings['revision']}版") + '，不得扩展用户授权）：\n' + settings['guidance'])

    def save(self, raw):
        request_id = raw.get('request_id')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise ContractError('routing_guidance_request_invalid')
        guidance = raw.get('guidance')
        if not isinstance(guidance, str) or len(guidance.encode('utf8')) > 24000 or '\x00' in guidance:
            raise ContractError('routing_guidance_text_invalid')
        from .agent_studio import redact
        value = {'guidance': redact(guidance)}
        for key, maximum in [('premium_routine_penalty', 1), ('experience_scale', 10)]:
            number = raw.get(key, DEFAULTS[key])
            if type(number) not in (int, float) or not 0 <= number <= maximum:
                raise ContractError('routing_guidance_weight_invalid')
            value[key] = number
        fingerprint = hashlib.sha256(encoded({**value, 'expected_revision': raw.get('expected_revision')}).encode()).hexdigest()
        with self.registry.transaction():
            c = self.registry._connect()
            old = c.execute('SELECT * FROM studio_routing_guidance_requests WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise ContractError('routing_guidance_request_conflict')
                return {**json.loads(old['result_json']), 'replayed': True}
            previous = self.settings(c)
            if raw.get('expected_revision') != previous['revision']:
                raise ContractError('routing_guidance_revision_conflict')
            c.execute('INSERT INTO studio_routing_guidance VALUES (?,?,?)',
                      (previous['revision'] + 1, encoded(value), utc_now()))
            result = {'ok': True, 'settings': self.settings(c), 'paid_request': False}
            c.execute('INSERT INTO studio_routing_guidance_requests VALUES (?,?,?)',
                      (request_id, fingerprint, encoded(result)))
        return result
