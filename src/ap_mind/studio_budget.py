"""Optional per-agent usage allowances. Unknown prices never prevent execution.

Price snapshots belong to requests; editing prices cannot rewrite old costs.
The ledger is local accounting, not a provider balance or a billing promise.
"""
from contextlib import closing
import hashlib
import json

from .contracts import ContractError, utc_now
from .studio_usage import normalize, number


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def policy(raw):
    result = {}
    for field in ('token_limit', 'amount_limit'):
        value = raw.get(field)
        if value is not None and number(value) is None:
            raise ContractError('agent_budget_' + field + '_invalid')
        result[field] = value
    prices = raw.get('prices') or {}
    if not isinstance(prices, dict):
        raise ContractError('agent_budget_prices_invalid')
    result['prices'] = {}
    for field in ('input', 'output', 'cache_read', 'cache_write'):
        value = prices.get(field)
        if value is not None and number(value) is None:
            raise ContractError('agent_budget_price_invalid')
        result['prices'][field] = value
    for field, default, maximum in (('currency', 'CNY', 12), ('price_source', '', 2048)):
        value = raw.get(field, default)
        if not isinstance(value, str) or len(value) > maximum or any(ord(ch) < 32 for ch in value):
            raise ContractError('agent_budget_' + field + '_invalid')
        result[field] = value
    return result


def estimate(usage, prices):
    if prices.get('input') is None or prices.get('output') is None:
        return 0, 'unpriced'
    if not usage.get('complete'):
        return 0, 'usage_unknown'
    cached = usage.get('cache_read_input_tokens') or 0
    written = usage.get('cache_creation_input_tokens') or 0
    uncached = max(0, usage['input_tokens'] - cached - written)
    read_price = prices.get('cache_read')
    write_price = prices.get('cache_write')
    value = (uncached * prices['input'] + usage['output_tokens'] * prices['output'] +
             cached * (prices['input'] if read_price is None else read_price) +
             written * (prices['input'] if write_price is None else write_price)) / 1_000_000
    return value, 'estimated'


class StudioBudget:
    def __init__(self, studio):
        self.studio, self.registry = studio, studio.registry
        with closing(self.registry._connect()) as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS studio_budget_policy(
                    agent_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS studio_usage_ledger(
                    request_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS studio_usage_agent ON studio_usage_ledger(agent_id);
                CREATE TABLE IF NOT EXISTS studio_budget_receipts(
                    request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL);
            ''')
            c.commit()

    def _policy(self, c, agent_id):
        row = c.execute('SELECT * FROM studio_budget_policy WHERE agent_id=?', (agent_id,)).fetchone()
        return {**policy({}), 'revision': 0} if not row else {**json.loads(row['payload_json']), 'revision': row['revision']}

    def status(self, agent_id, c=None):
        if c is None:
            with closing(self.registry._connect()) as connection:
                return self.status(agent_id, connection)
        settings = self._policy(c, agent_id)
        totals=c.execute('''SELECT COUNT(*) AS requests,
            COALESCE(SUM(json_extract(payload_json,'$.usage.total_tokens')),0) AS tokens,
            COALESCE(SUM(CASE WHEN json_extract(payload_json,'$.usage.complete')=1 THEN 0 ELSE 1 END),0) AS unknown,
            COALESCE(SUM(CASE WHEN json_extract(payload_json,'$.estimate_state')='estimated' THEN 0 ELSE 1 END),0) AS unestimated
            FROM studio_usage_ledger WHERE agent_id=?''',(agent_id,)).fetchone()
        tokens = totals['tokens']
        amounts={r['currency']:r['amount'] for r in c.execute('''SELECT json_extract(payload_json,'$.currency') AS currency,
            SUM(json_extract(payload_json,'$.amount')) AS amount FROM studio_usage_ledger WHERE agent_id=? GROUP BY currency''',(agent_id,))}
        amount = amounts.get(settings['currency'], 0)
        priced = settings['prices']['input'] is not None and settings['prices']['output'] is not None
        token_left = max(0, settings['token_limit'] - tokens) if settings['token_limit'] is not None else None
        amount_left = max(0, settings['amount_limit'] - amount) if settings['amount_limit'] is not None and priced else None
        exhausted = token_left == 0 or amount_left == 0
        ratios = [max(0, min(1, left / cap)) if cap else 0 for left, cap in
                  ((token_left, settings['token_limit']), (amount_left, settings['amount_limit'])) if left is not None]
        return {**settings, 'agent_id': agent_id, 'tokens_used': tokens, 'amount_used': amount,
                'amounts_by_currency': amounts, 'token_remaining': token_left, 'amount_remaining': amount_left,
                'saturation': min(ratios) if ratios else None, 'exhausted': exhausted,
                'state': 'hungry' if exhausted else 'available', 'request_count': totals['requests'],
                'unknown_usage_requests': totals['unknown'],
                'unestimated_requests': totals['unestimated'],
                'price_status': 'estimated' if priced else 'unpriced',
                'enforcement': 'between_model_requests_and_runs', 'provider_balance': None}

    def save(self, raw, feed=False):
        request_id, agent_id = raw.get('request_id'), raw.get('agent_id')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise ContractError('agent_budget_request_id_required')
        fingerprint = hashlib.sha256(encoded({'feed': feed, **raw}).encode()).hexdigest()
        with self.registry.transaction():
            c = self.registry._connect()
            prior = c.execute('SELECT * FROM studio_budget_receipts WHERE request_id=?', (request_id,)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise ContractError('agent_budget_request_conflict')
                return {**json.loads(prior['result_json']), 'replayed': True}
            if not c.execute('SELECT 1 FROM studio_agents WHERE agent_id=?', (agent_id,)).fetchone():
                raise ContractError('agent_not_found')
            current = self._policy(c, agent_id)
            if raw.get('expected_revision') != current['revision']:
                raise ContractError('agent_budget_revision_conflict')
            if feed:
                updated = policy(current)
                additions = [raw.get('tokens', 0), raw.get('amount', 0)]
                if any(number(n) is None for n in additions) or not any(additions):
                    raise ContractError('agent_budget_feed_positive_required')
                for field, extra in zip(('token_limit', 'amount_limit'), additions):
                    if extra:
                        if updated[field] is None:
                            raise ContractError('agent_budget_feed_requires_limit')
                        updated[field] += extra
            else:
                updated = policy(raw)
            updated['updated_at'] = utc_now()
            c.execute('INSERT OR REPLACE INTO studio_budget_policy VALUES (?,?,?)',
                      (agent_id, current['revision'] + 1, encoded(updated)))
            result = {'ok': True, 'budget': self.status(agent_id, c), 'paid_request': False}
            c.execute('INSERT INTO studio_budget_receipts VALUES (?,?,?)', (request_id, fingerprint, encoded(result)))
        return result

    def feed(self, raw):
        return self.save(raw, feed=True)

    def check(self, agent_id, c=None):
        status = self.status(agent_id, c)
        return '已达到你设置的用量上限，任务已保留；投喂或调整上限后继续。' if status['exhausted'] else None

    def observe(self, agent_id, run_id, request_id, raw=None, protocol='anthropic'):
        usage = normalize(raw, protocol)
        with self.registry.transaction():
            c = self.registry._connect()
            prior = c.execute('SELECT * FROM studio_usage_ledger WHERE request_id=?', (request_id,)).fetchone()
            if prior and (prior['agent_id'] != agent_id or prior['run_id'] != run_id):
                raise ContractError('agent_usage_identity_conflict')
            snapshot = json.loads(prior['payload_json']) if prior else self._policy(c, agent_id)
            # Late submitted/missing-usage events cannot erase a settled count.
            if prior and snapshot['usage'].get('complete') and not usage['complete']:
                return
            amount, state = estimate(usage, snapshot['prices'])
            value = {'usage': usage, 'prices': snapshot['prices'], 'currency': snapshot['currency'],
                     'amount': amount, 'estimate_state': state, 'updated_at': utc_now()}
            c.execute('INSERT OR REPLACE INTO studio_usage_ledger VALUES (?,?,?,?)',
                      (request_id, agent_id, run_id, encoded(value)))
