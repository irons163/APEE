"""Bounded repair tasks for exact, owned PR revisions and verified CI logs."""

import hashlib
import json
from pathlib import Path

from .config import ApeeError
from .service import atomic_write
from .store import now
from .github import CINotStarted


def schedule(engine):
    if not engine.config.ci_repair['enabled']:
        return dict(added=[], errors=[])
    added, errors = [], []
    for task in engine.store.tasks():
        if (task['state'] != 'review' or task['spec'].get('source') == 'ci_repair'
                or not engine.config.projects.get(task['repo'], {}).get('enabled')):
            continue
        run = engine.store.run(task['id'])
        status = run.get('pr_status', {})
        if (run.get('pr_refresh_error') or status.get('ci') != 'failed' or status.get('state') != 'OPEN'
                or not status.get('matches_verified_commit') or not status.get('matches_delivery_target')):
            continue
        key = 'ci_repair:' + task['id']
        control = engine.store.control(key) or dict(tasks=[])
        active = [identifier for identifier in control['tasks'] if engine.store.task(identifier)['state'] in
                  ('queued', 'preparing', 'running', 'testing', 'ready', 'publishing')]
        if active:
            engine.store.set_control('ci_repair_status:'+task['id'], dict(kind='in_progress', tasks=active, message='CI 修復任務已在處理或等待交付。'))
            continue
        if len(control['tasks']) >= engine.config.ci_repair['max_attempts_per_pr']:
            engine.store.set_control('ci_repair_status:'+task['id'], dict(kind='limit', message='已達這個 PR 的 CI 修復累計上限。', tasks=control['tasks']))
            continue
        try:
            logs = engine.github.failure_logs(task['repo'], run['pr_url'], status['head'])
            spec = dict(source='ci_repair', parent_task=task['id'], parent_run=run['id'], pr_url=run['pr_url'],
                        expected_head=status['head'], branch=run['branch'], base=run['base'], ci_logs=logs,
                        title='修復 CI：' + task['spec']['title'], body=task['spec']['body'] +
                        '\n\n修復下面與目前 PR 版本相符的 CI 失敗。保留原完成條件與檢查，不能削弱測試、工作流程或權限。\n' +
                        json.dumps(logs, ensure_ascii=False))
            number = len(control['tasks'])+1
            spec['ci_repair_attempt'] = number
            identifier = task['repo'] + '@ci-' + hashlib.sha256((task['id'] + status['head'] + ':' + str(number)).encode()).hexdigest()[:16]
            directory = engine.config.state_dir/'ci-evidence'/identifier.split('@')[-1]
            atomic_write(directory/'evidence.json', (json.dumps(spec, ensure_ascii=False, indent=2)+'\n').encode())
            with engine.store.db:
                changed = engine.store.db.execute("INSERT OR IGNORE INTO tasks VALUES (?, ?, ?, 'queued', ?, ?, ?, NULL)",
                    (identifier, task['repo'], json.dumps(spec, ensure_ascii=False), task['priority'], now(), now())).rowcount
                if changed:
                    engine.store._event(identifier, 'queued', dict(source='ci_repair', parent=task['id']))
                    control['tasks'].append(identifier)
                    engine.store._set_control(key, control)
                    added.append(identifier)
            engine.store.set_control('ci_repair_status:'+task['id'], dict(kind='scheduled', task=identifier, message='已加入版本核對過的 CI 修復任務。'))
        except ApeeError as exc:
            engine.store.set_control('ci_repair_status:'+task['id'], dict(kind='platform_blocked' if isinstance(exc, CINotStarted) else 'logs_unavailable', message=str(exc), at=now()))
            errors.append(dict(task=task['id'], error=str(exc)))
    return dict(added=added, errors=errors)


def assert_source(engine, spec, repo):
    parent = engine.store.task(spec['parent_task'])
    if parent['repo'] != repo or parent['run_id'] != spec['parent_run']:
        raise ApeeError('CI 修復的原始任務版本已改變。')
    data = engine.github.pr_status(repo, spec['pr_url'])
    if (data['state'] != 'OPEN' or data['headRefOid'] != spec['expected_head']
            or data['headRefName'] != spec['branch'] or data['baseRefName'] != spec['base']):
        raise ApeeError('CI 修復的 PR 來源、版本或狀態已改變。')
    return data


def publish(engine, task, run):
    spec = run['spec']
    parent_task = engine.store.task(spec['parent_task'])
    if parent_task['repo'] != task['repo'] or parent_task['run_id'] != spec['parent_run']:
        raise ApeeError('CI 修復的原始交付紀錄已改變。')
    data = engine.github.pr_status(task['repo'], spec['pr_url'])
    if (data['state'] != 'OPEN' or data['headRefName'] != spec['branch'] or data['baseRefName'] != spec['base']):
        raise ApeeError('CI 修復交付目標已改變或關閉。')
    if data['headRefOid'] not in (spec['expected_head'], run['commit']):
        raise ApeeError('PR 已有其他提交，未覆寫。')
    if data['headRefOid'] != run['commit']:
        engine.github.push_update(task['repo'], Path(run['workspace']), run['branch'], spec['expected_head'], run['commit'])
    confirmed = engine.github.pr_status(task['repo'], spec['pr_url'])
    if (confirmed['headRefOid'] != run['commit'] or confirmed['state'] != 'OPEN'
            or confirmed['headRefName'] != spec['branch'] or confirmed['baseRefName'] != spec['base']):
        raise ApeeError('CI 修復的遠端更新結果尚未核對。')
    run['pr_url'] = spec['pr_url']
    run.pop('publish_error', None)
    parent = engine.store.run(spec['parent_task'])
    if parent.get('pr_status'):
        parent.setdefault('prior_delivery_observations', []).append(dict(status=parent.pop('pr_status'),
            observed_at=parent.get('pr_observed_at'), replaced_by=run['id']))
    parent.pop('pr_refresh_error', None)
    parent['active_delivery'] = dict(run_id=run['id'], commit=run['commit'], verified_at=run['verified_at'],
                                     quality_required=True, source='ci_repair')
    with engine.store.db:
        engine.store._transition(task['id'], 'review', dict(event='ci_repair_published', url=run['pr_url']), run)
        engine.store._transition(spec['parent_task'], 'review', dict(event='ci_repaired', commit=run['commit']), parent)
    return run
