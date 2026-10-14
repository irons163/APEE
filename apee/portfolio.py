"""Persistent goals, evidence, dependency decisions and measured outcomes."""

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from .config import ApeeError, positive_int, repo_name
from .store import now


ACTIVE = {'preparing', 'running', 'testing', 'ready', 'publishing', 'review'}


def text(value, name, limit=6000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ApeeError(f'{name} 需要非空文字，最多 {limit} 字。')
    return value.strip()


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat(timespec='seconds')
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApeeError('觀測時間需要包含時區的 ISO 8601 時間。') from exc


def number(value):
    if not isinstance(value, str) or not re.fullmatch(r'-?\d{1,12}(?:\.\d{1,9})?', value):
        raise ApeeError('指標數值需要有限的十進位字串。')
    return str(Decimal(value))


class Portfolio:
    def __init__(self, engine):
        self.engine, self.store, self.config = engine, engine.store, engine.config

    def save(self, kind, data):
        if not isinstance(data, dict):
            raise ApeeError('需要完整的資料物件。')
        identifier = data.get('id') or uuid.uuid4().hex
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identifier):
            raise ApeeError('資料識別碼無法辨識。')
        allowed = {
            'goals': {'title', 'objective', 'criteria', 'repos', 'priority', 'enabled', 'autonomy'},
            'feedback': {'goal', 'repo', 'text', 'source', 'observed_at'},
            'definitions': {'goal', 'repo', 'name', 'unit', 'direction', 'target'},
            'metrics': {'definition', 'value', 'source', 'observed_at'},
        }.get(kind)
        if allowed is None or set(data)-allowed-{'id', 'expected_updated'}:
            raise ApeeError('未知的目標或觀測欄位。')
        previous = next((item for item in self.store.documents(kind) if item['id']==identifier), None)
        if previous and data.get('expected_updated') != previous['updated']:
            raise ApeeError('資料已改變，請重新載入後再儲存。')
        if previous and kind in ('feedback', 'metrics'):
            raise ApeeError('原始觀測不能覆寫；請新增更正觀測並注明來源。')
        item = {key: value for key, value in data.items() if key in allowed}
        if kind == 'goals':
            for key in ('title', 'objective', 'criteria'): item[key] = text(item.get(key), key)
            repos = item.get('repos')
            if (not isinstance(repos, list) or not repos or len(repos)>100
                    or any(not isinstance(repo, str) for repo in repos) or len(set(repos))!=len(repos)):
                raise ApeeError('目標需要不重複的專案清單。')
            item['repos'] = sorted({repo_name(repo) for repo in repos})
            if any(repo not in self.config.projects for repo in item['repos']): raise ApeeError('目標專案尚未設定。')
            if len(item['repos']) != len(repos): raise ApeeError('目標專案不能重複。')
            item['priority'] = positive_int(item.get('priority', 3), '目標優先序', 5)
            if type(item.get('enabled', True)) is not bool or item.get('autonomy', 'propose') not in ('propose', 'enqueue'):
                raise ApeeError('目標啟用與自動入列政策無法辨識。')
            item.setdefault('enabled', True)
            item.setdefault('autonomy', 'propose')
        elif kind in ('feedback', 'definitions'):
            goal = self.store.document('goals', item.get('goal'))
            item['repo'] = repo_name(item.get('repo'))
            if item['repo'] not in goal['repos']:
                raise ApeeError('觀測專案不屬於這個目標。')
            if kind == 'feedback':
                for key in ('text', 'source'): item[key] = text(item.get(key), key)
                item['observed_at'] = timestamp(item.get('observed_at', now()))
                if item['observed_at']>now(): raise ApeeError('回饋觀測時間不能在未來。')
            else:
                for key in ('name', 'unit'): item[key] = text(item.get(key), key, 200)
                if item.get('direction') not in ('gte', 'lte'):
                    raise ApeeError('指標方向需要 gte 或 lte。')
                item['target'] = number(item.get('target'))
                # Changed definitions invalidate comparisons, so version them instead.
                if previous and any(item.get(key)!=previous.get(key) for key in allowed):
                    raise ApeeError('指標定義不能改寫；請建立新的定義以保留比較依據。')
        else:
            self.store.document('definitions', item.get('definition'))
            item['value'] = number(item.get('value'))
            item['source'] = text(item.get('source'), 'source')
            item['observed_at'] = timestamp(item.get('observed_at', now()))
            if item['observed_at']>now(): raise ApeeError('指標觀測時間不能在未來。')
            for old in self.store.documents('metrics'):
                if old['definition']==item['definition'] and old['observed_at']==item['observed_at']:
                    if old['value']!=item['value']: raise ApeeError('同一指標與時間已有不同觀測；請提供新的觀測時間。')
                    if old['source']==item['source']: return old
        item.update(id=identifier, created=previous['created'] if previous else now(), updated=uuid.uuid4().hex)
        self.store.save_document(kind, item)
        return item

    def links(self):
        return {item['id']: item for item in self.store.documents('task_context')}

    def link(self, data):
        if not isinstance(data, dict) or set(data)-{'task', 'goal', 'depends_on'}:
            raise ApeeError('任務連結欄位無法辨識。')
        task = self.store.task(data.get('task'))
        if task['state'] != 'queued':
            raise ApeeError('只能修改尚未開始的任務依賴。')
        goal = self.store.document('goals', data.get('goal')) if data.get('goal') else None
        if goal and task['repo'] not in goal['repos']:
            raise ApeeError('任務專案不屬於這個目標。')
        deps = data.get('depends_on', [])
        if (not isinstance(deps, list) or len(deps)>100 or any(not isinstance(x, str) for x in deps)
                or len(deps)!=len(set(deps)) or task['id'] in deps):
            raise ApeeError('任務依賴不能重複或依賴自己。')
        for dep in deps: self.store.task(dep)
        item = dict(id=task['id'], goal=goal['id'] if goal else None, depends_on=deps, updated=now())
        links = self.links()
        links[item['id']] = item
        remaining = {identifier: set(link.get('depends_on', [])) & set(links) for identifier, link in links.items()}
        while remaining:
            ready = {identifier for identifier, deps in remaining.items() if not deps}
            if not ready: raise ApeeError('依賴形成循環，未儲存。')
            remaining = {identifier: deps-ready for identifier, deps in remaining.items() if identifier not in ready}
        self.store.save_document('task_context', item)
        return item

    def completion(self, identifier):
        task = self.store.task(identifier)
        if task['state']=='merged' and task['run_id']:
            run = self.store.run(identifier)
            status = run.get('pr_status', {})
            return (status.get('matches_verified_commit') is True and status.get('matches_delivery_target') is True
                    and not run.get('pr_refresh_error'))
        local = self.links().get(identifier, {}).get('local_completion')
        return bool(local and task['state']=='ready' and task['run_id'] and local['commit']==self.store.run(identifier)['commit'])

    def complete_local(self, data):
        task = self.store.task(data.get('task'))
        if task['state'] != 'ready': raise ApeeError('只有通過驗證的本機成果能標記完成。')
        run = self.store.run(task['id'])
        self.engine.check_delivery(task, run)
        item = self.links().get(task['id'], dict(id=task['id'], goal=None, depends_on=[]))
        item['local_completion'] = dict(commit=run['commit'], at=now(), source='user', reason=text(data.get('reason'), '完成依據'))
        self.store.save_document('task_context', item)
        return item

    def decisions(self):
        tasks = self.store.tasks()
        active = {t['repo'] for t in tasks if t['state'] in ACTIVE and not self.completion(t['id'])
                  and self.config.projects.get(t['repo'], {}).get('enabled')}
        links = self.links()
        goals = {g['id']: g for g in self.store.documents('goals')}
        result = []
        for task in tasks:
            if task['state']!='queued': continue
            reasons = []
            project = self.config.projects.get(task['repo'], {})
            if not project.get('enabled'): reasons.append('專案尚未啟用')
            link = links.get(task['id'], {})
            goal = goals.get(link.get('goal'))
            if goal and not goal['enabled']: reasons.append('目標已停用')
            if goal and task['repo'] not in goal['repos']: reasons.append('專案已不屬於這個目標')
            for dep in link.get('depends_on', []):
                if not self.completion(dep): reasons.append('等待依賴成果：'+dep)
            if task['repo'] not in active and len(active)>=self.config.planning['max_active_projects']:
                reasons.append('已達同時推進專案上限')
            result.append(dict(task=task['id'], eligible=not reasons, reasons=reasons,
                goal=link.get('goal'), priority=[goal['priority'] if goal else 3, project.get('priority', 3), task['priority']],
                created=task['created']))
        result.sort(key=lambda x: (*x['priority'], x['created'], x['task']))
        return dict(active_projects=sorted(active), limit=self.config.planning['max_active_projects'], tasks=result,
                    next_task=next((x['task'] for x in result if x['eligible']), None))

    def latest_metrics(self):
        latest = {}
        for item in self.store.documents('metrics'):
            if item['observed_at']>now(): continue
            old = latest.get(item['definition'])
            if old is None or (item['observed_at'], item['created'], item['id'])>(old['observed_at'], old['created'], old['id']):
                latest[item['definition']] = item
        return latest

    def context(self):
        candidates = self.store.documents('candidates')
        outcomes = {item['candidate']: item for item in self.store.documents('outcomes')}
        awaiting = {item['goal'] for item in candidates if item['state']=='pending' or
                    (item['state']=='accepted' and outcomes.get(item['id'], {}).get('status', 'unknown')=='unknown')}
        goals = [g for g in self.store.documents('goals') if g['enabled'] and g['id'] not in awaiting
                 and any(self.config.projects.get(repo, {}).get('enabled') for repo in g['repos'])]
        goal_ids = {g['id'] for g in goals}
        latest = self.latest_metrics()
        definitions = [dict(item, latest=latest.get(item['id'])) for item in self.store.documents('definitions') if item['goal'] in goal_ids]
        return dict(goals=goals, feedback=[f for f in self.store.documents('feedback') if f['goal'] in goal_ids],
            definitions=definitions, projects=[dict(repo=p['repo'], allowed_paths=p['allowed_paths'], tests=p['tests'])
                for p in self.config.projects.values() if p['enabled']],
            prior_candidates=[dict(id=c['id'], title=c['title'], repo=c['repo'], goal=c['goal'], state=c['state']) for c in candidates[-100:]])

    def propose(self, proposals, context, job_id):
        maximum = self.config.planning['max_new_tasks_per_cycle']
        if (not isinstance(proposals, dict) or set(proposals)!={'proposals'}
                or not isinstance(proposals['proposals'], list) or len(proposals['proposals'])>maximum):
            raise ApeeError('規劃結果超出數量上限或格式不完整。')
        goals = {g['id']: g for g in context['goals']}
        sources = {g['id']: g['id'] for g in context['goals']}
        sources.update({f['id']: f['goal'] for f in context['feedback']})
        for definition in context['definitions']:
            sources[definition['id']] = definition['goal']
            if definition.get('latest'): sources[definition['latest']['id']] = definition['goal']
        prepared = []
        for proposal in proposals['proposals']:
            if not isinstance(proposal, dict) or set(proposal)!={'goal', 'repo', 'title', 'body', 'acceptance', 'sources', 'reason'}:
                raise ApeeError('候選工作欄位不完整。')
            goal = goals.get(proposal['goal'])
            repo = repo_name(proposal['repo'])
            evidence = proposal['sources']
            if (goal is None or repo not in goal['repos'] or not self.config.project(repo)
                    or not isinstance(evidence, list) or not evidence or len(evidence)>20
                    or any(not isinstance(x, str) or sources.get(x)!=goal['id'] for x in evidence)):
                raise ApeeError('候選工作的目標、專案或證據來源無法核對。')
            item = dict(proposal, repo=repo)
            for key in ('title', 'body', 'acceptance', 'reason'): item[key] = text(item[key], key)
            # The same goal and title cannot be emitted again just by changing source IDs.
            identifier = hashlib.sha256((goal['id']+'\n'+repo+'\n'+item['title'].casefold()).encode()).hexdigest()[:32]
            if any(c['id']==identifier for c in self.store.documents('candidates')): continue
            item.update(id=identifier, state='pending', created=now(), job_id=job_id,
                baseline=[d['latest'] for d in context['definitions'] if d['goal']==goal['id'] and d.get('latest')])
            prepared.append(item)
        remaining = self.config.planning['max_pending_candidates'] - sum(c['state']=='pending' for c in self.store.documents('candidates'))
        if len(prepared)>remaining: raise ApeeError('候選工作已達等待上限。')
        with self.store.db:
            for item in prepared: self.store._save_document('candidates', item)
        return prepared

    def decide(self, data):
        item = self.store.document('candidates', data.get('candidate'))
        decision = data.get('decision')
        if decision not in ('accept', 'reject'): raise ApeeError('候選決策需要 accept 或 reject。')
        if item['state']!='pending':
            if (item['state'], decision) in (('accepted', 'accept'), ('rejected', 'reject')): return item
            raise ApeeError('候選工作已有不同決策，請重新載入。')
        reason = text(data.get('reason'), '決策原因')
        if decision=='accept':
            goal = self.store.document('goals', item['goal'])
            if not goal['enabled']: raise ApeeError('目標已停用，未加入工作。')
            self.config.project(item['repo'])
            spec = dict(source='proposal', title=item['title'], body=item['body']+'\n\n完成條件：\n'+item['acceptance'],
                        candidate=item['id'], goal=item['goal'], sources=item['sources'], reason=item['reason'])
            task_id = item['repo']+'@proposal-'+item['id']
            stamp = now()
            with self.store.db:
                changed = self.store.db.execute("INSERT OR IGNORE INTO tasks VALUES (?, ?, ?, 'queued', ?, ?, ?, NULL)",
                    (task_id, item['repo'], json.dumps(spec, ensure_ascii=False), goal['priority'], stamp, stamp)).rowcount
                # Capture current measurements after the write transaction starts.
                latest = self.latest_metrics()
                item['planning_baseline'] = item['baseline']
                item['baseline'] = [latest[d['id']] for d in self.store.documents('definitions')
                                    if d['goal']==item['goal'] and d['id'] in latest]
                if changed:
                    self.store._event(task_id, 'queued', dict(source='proposal', candidate=item['id'], reason=reason))
                    self.store._save_document('task_context', dict(id=task_id, goal=item['goal'], depends_on=[]))
                item.update(state='accepted', task=task_id, decision_at=stamp, decision_reason=reason)
                self.store._save_document('candidates', item)
        else:
            item.update(state='rejected', decision_at=now(), decision_reason=reason)
            self.store.save_document('candidates', item)
        return item

    def measure(self):
        latest = self.latest_metrics()
        results = []
        for item in self.store.documents('candidates'):
            if item['state']!='accepted': continue
            task = self.store.task(item['task'])
            previous = next((o for o in self.store.documents('outcomes') if o['candidate']==item['id']), {})
            delivered = self.completion(task['id'])
            # Persist the delivery observation before accepting later metric evidence.
            if delivered:
                local = self.links().get(task['id'], {}).get('local_completion', {})
                merged = self.store.run(task['id']).get('pr_status', {}).get('merged_at')
                delivery_time = timestamp(merged) if merged else local.get('at') or now()
            else: delivery_time = None
            delivered_at = previous.get('delivered_at') or delivery_time
            comparisons = []
            for baseline in item['baseline']:
                after = latest.get(baseline['definition'])
                definition = self.store.document('definitions', baseline['definition'])
                if (not delivered or not after or after['id']==baseline['id'] or
                        after['observed_at']<=delivered_at or after['observed_at']<=baseline['observed_at']): continue
                before_value, after_value, target = Decimal(baseline['value']), Decimal(after['value']), Decimal(definition['target'])
                up = definition['direction']=='gte'
                comparisons.append(dict(definition=definition['id'], before=baseline['id'], after=after['id'],
                    before_value=str(before_value), after_value=str(after_value),
                    achieved=after_value>=target if up else after_value<=target,
                    regressed=after_value<before_value if up else after_value>before_value))
            status = 'unknown'
            if comparisons and any(c['regressed'] for c in comparisons): status = 'regressed'
            elif comparisons and len(comparisons)==len(item['baseline']) and all(c['achieved'] for c in comparisons): status = 'confirmed'
            elif comparisons: status = 'not_met'
            result = dict(id=item['id'], candidate=item['id'], task=task['id'], delivered=delivered,
                delivered_at=delivered_at, status=status, comparisons=comparisons,
                reason='需要交付後的可比較指標觀測' if status=='unknown' else '依指標定義與觀測比較；不等同因果歸因')
            if result!=previous: self.store.save_document('outcomes', result)
            results.append(result)
        return results

    def snapshot(self):
        return {kind: self.store.documents(kind) for kind in ('goals', 'feedback', 'definitions', 'metrics', 'candidates', 'outcomes')} | dict(
            links=list(self.links().values()), scheduling=self.decisions(), jobs=self.store.model_jobs())
