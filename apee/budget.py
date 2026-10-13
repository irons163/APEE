"""Observed usage and admission budgets; no promise of an upstream billing cap."""

import json
from decimal import Decimal, ROUND_UP
from pathlib import Path

from .config import ApeeError, money
from .leases import lease_state
from .store import now


def token_usage(values):
    if not isinstance(values, list) or not values:
        return None
    total = dict(input_tokens=0, cached_input_tokens=0, output_tokens=0, cache_write_input_tokens=0)
    for value in values:
        if not isinstance(value, dict):
            return None
        normalized = dict(value, cache_write_input_tokens=value.get('cache_write_input_tokens', 0))
        if any(type(normalized.get(key)) is not int or not 0 <= normalized[key] <= 10**12 for key in total):
            return None
        if value['cached_input_tokens'] > value['input_tokens']:
            return None
        for key in total:
            total[key] += normalized[key]
    total['total_tokens'] = total['input_tokens'] + total['output_tokens']
    return total


def log_usage(path):
    values, pending, failed = [], 0, False
    try:
        for line in Path(path).read_text(encoding='utf-8', errors='replace').splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                if line.lstrip().startswith('{'):
                    failed = True
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get('type')
            if kind == 'turn.started':
                pending += 1
            elif kind == 'turn.completed':
                pending = max(0, pending - 1)
                values.append(event.get('usage'))
            elif kind in ('turn.failed', 'error'):
                failed = True
    except OSError:
        return None
    return token_usage(values) if not pending and not failed else None


def estimate(usage, prices):
    if usage is None or not prices or usage.get('cache_write_input_tokens', 0):
        return None
    cost = ((usage['input_tokens'] - usage['cached_input_tokens']) * money(prices['input'], 'input')
            + usage['cached_input_tokens'] * money(prices['cached_input'], 'cached_input')
            + usage['output_tokens'] * money(prices['output'], 'output')) / Decimal(1_000_000)
    return str(cost.quantize(Decimal('0.000000001'), rounding=ROUND_UP))


class Budget:
    def __init__(self, config, store):
        self.config, self.store = config, store

    def begin(self, run):
        record = dict(run_id=run['id'], at=now(), model=self.config.model,
                      prices=self.config.budget.get('prices_per_million'), status='pending',
                      usage=None, estimated_usd=None, source='model_started')
        self.store.save_usage(record)

    def finish(self, run):
        record = next(item for item in self.store.usage_records() if item['run_id'] == run['id'])
        usage = log_usage(self.config.state_dir / 'runs' / run['id'] / 'agent-events.log')
        record.update(status='known' if usage is not None else 'unknown', usage=usage,
                      estimated_usd=estimate(usage, record['prices']), source='codex_jsonl')
        self.store.save_usage(record)

    def records(self):
        records = {item['run_id']: item for item in self.store.usage_records()}
        for run in self.store.runs():
            if run['id'] in records or run.get('agent', {}).get('backend') == 'imported_patch':
                continue
            directory = self.config.state_dir / 'runs' / run['id']
            if (run.get('agent', {}).get('backend') != 'codex'
                    and not (directory / 'agent-events.log').exists() and not (directory / 'prompt.txt').exists()):
                continue
            usage = log_usage(directory / 'agent-events.log')
            records[run['id']] = dict(run_id=run['id'], at=run['started_at'], model=None, prices=None,
                                      status='known' if usage is not None else 'unknown', usage=usage,
                                      estimated_usd=None, source='legacy_codex_jsonl')
        return list(records.values())

    def reconcile(self):
        stored = {item['run_id']: item for item in self.store.usage_records()}
        for record in self.records():
            if record['run_id'] not in stored:
                self.store.save_usage(record)
            elif record['status'] == 'pending' and lease_state(self.config.state_dir / 'runs' / record['run_id']) == 'idle':
                self.finish(dict(id=record['run_id']))

    def summary(self):
        day = now()[:10]
        records = [item for item in self.records() if item['at'][:10] == day]
        known = [item for item in records if item['usage'] is not None]
        unknown = [item['run_id'] for item in records if item['usage'] is None]
        unpriced = [item['run_id'] for item in records if item['estimated_usd'] is None]
        cost = sum((Decimal(item['estimated_usd']) for item in records if item['estimated_usd'] is not None), Decimal(0))
        return dict(date=day, tokens=sum(item['usage']['total_tokens'] for item in known),
                    input_tokens=sum(item['usage']['input_tokens'] for item in known),
                    cached_input_tokens=sum(item['usage']['cached_input_tokens'] for item in known),
                    output_tokens=sum(item['usage']['output_tokens'] for item in known),
                    unknown_runs=unknown, unpriced_runs=unpriced, estimated_usd=str(cost) if not unpriced else None,
                    known_estimated_usd=str(cost), limits=self.config.budget, records=records,
                    policy='admission_only', notice='在開始新模型回合前檢查；進行中的回合可能超額。費用為設定單價的估算，並非帳單。')

    def blocker(self):
        limits = self.config.budget
        if not ('daily_tokens' in limits or 'daily_estimated_usd' in limits):
            return None
        summary = self.summary()
        if summary['unknown_runs']:
            return dict(kind='usage_unknown', message='尚有無法核對的模型用量；核對後才能開始新模型回合。', runs=summary['unknown_runs'])
        if 'daily_tokens' in limits and summary['tokens'] >= limits['daily_tokens']:
            return dict(kind='token_budget', message='已達今日 token 預算，停止新的模型派工。', used=summary['tokens'], limit=limits['daily_tokens'])
        if 'daily_estimated_usd' in limits:
            if summary['unpriced_runs']:
                return dict(kind='cost_unknown', message='尚有未定價的模型回合；費用不視為零。', runs=summary['unpriced_runs'])
            if Decimal(summary['estimated_usd']) >= Decimal(limits['daily_estimated_usd']):
                return dict(kind='cost_budget', message='已達今日估算費用預算，停止新的模型派工。', used=summary['estimated_usd'], limit=limits['daily_estimated_usd'])
        return None

    def record_manual(self, run_id, usage, estimated_usd, reason):
        if not reason.strip():
            raise ApeeError('人工核對需要原因。')
        run = next((item for item in self.store.runs() if item['id'] == run_id), None)
        if not run:
            raise ApeeError('找不到指定嘗試。')
        previous = next((item for item in self.records() if item['run_id'] == run_id), None)
        if run.get('execution_backend') == 'imported_patch' or (not previous and run.get('execution_backend') != 'codex'):
            raise ApeeError('此嘗試沒有模型用量來源。')
        lease = lease_state(self.config.state_dir / 'runs' / run_id)
        legacy_complete = (lease == 'missing' and run.get('lease_version') is None
                           and previous and previous['status'] == 'known')
        if lease != 'idle' and not legacy_complete:
            raise ApeeError('只能核對執行鎖已釋放的嘗試。')
        usage = token_usage([usage])
        if usage is None:
            raise ApeeError('token 用量無法辨識。')
        cost = str(money(estimated_usd, 'estimated_usd')) if estimated_usd is not None else None
        if previous and previous['status'] == 'known':
            if any(previous['usage'][key] != usage[key] for key in ('input_tokens', 'cached_input_tokens', 'output_tokens')):
                raise ApeeError('已有可核對的 token 用量，未以人工值覆寫。')
            usage = previous['usage']
        record = dict(previous or {}, run_id=run_id, at=previous['at'] if previous else run['started_at'],
                      model=(previous or {}).get('model'), prices=(previous or {}).get('prices'), status='known',
                      usage=usage, estimated_usd=cost, source='manual',
                      manual=dict(at=now(), reason=reason, previous=previous))
        self.store.save_usage(record)
        return record
