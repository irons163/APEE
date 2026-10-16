"""A single local snapshot and validated settings edits shared with the desktop."""

import json
import tempfile
import hashlib
from pathlib import Path

from . import __version__
from .budget import Budget
from .config import ApeeError, Config, repo_name
from .report import STATES, review_description
from .service import Service, atomic_write
from .store import now
from .portfolio import Portfolio
from .notifications import Notifications


def latest_config(config):
    latest = Config(config.path)
    if latest.state_dir != config.state_dir:
        raise ApeeError('狀態目錄已改變，未修改設定。')
    return latest


def save_config(config, data):
    with tempfile.NamedTemporaryFile(dir=config.path.parent, suffix='.json', delete=False) as output:
        temporary = Path(output.name)
    try:
        atomic_write(temporary, (json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2)+'\n').encode())
        Config(temporary)
        temporary.replace(config.path)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(saved=True, config=str(config.path))


def configure(config, changes):
    if not isinstance(changes, dict) or set(changes) - {'codex', 'model', 'budget', 'auto_recovery', 'quality', 'ci_repair', 'planning', 'notifications'}:
        raise ApeeError('設定欄位無法辨識。')
    if 'codex' in changes and isinstance(changes['codex'], str) and '/' in changes['codex'] and not Path(changes['codex']).is_absolute():
        raise ApeeError('Codex CLI 請使用執行檔名稱或絕對路徑。')
    latest = latest_config(config)
    return save_config(config, dict(latest.data, **changes))


def add_project(config, project):
    latest = latest_config(config)
    name = repo_name(project['repo'])
    if name in latest.projects:
        raise ApeeError('專案已有設定，請直接編輯設定檔以保留既有選項。')
    project = dict(project, repo=name, enabled=True, issue_label='apee:ready')
    data = dict(latest.data, projects=[*latest.data.get('projects', []), project])
    save_config(config, data)
    return dict(repo=name, config=str(config.path))


def project_revision(project):
    return hashlib.sha256(json.dumps(project, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def edit_project(config, name, changes, expected_revision):
    latest = latest_config(config)
    name = repo_name(name)
    if (not isinstance(changes, dict) or set(changes)-{'enabled', 'priority', 'issue_label', 'publish_draft', 'setup_network', 'allowed_paths', 'tests', 'setup', 'base_branch'}):
        raise ApeeError('專案修改欄位無法辨識。')
    projects = latest.data.get('projects', [])
    previous = next((item for item in projects if repo_name(item['repo'])==name), None)
    if previous is None or project_revision(previous)!=expected_revision:
        raise ApeeError('專案設定已改變，請重新載入後再儲存。')
    updated = dict(previous, **changes)
    save_config(latest, dict(latest.data, projects=[updated if item is previous else item for item in projects]))
    return dict(repo=name, saved=True)


def snapshot(engine):
    budget = Budget(engine.config, engine.store)
    tasks = []
    runs = {run['id']: run for run in engine.store.runs()}
    for task in engine.store.tasks():
        run = engine.store.run(task['id']) if task['run_id'] else {}
        verified = runs.get(run.get('active_delivery', {}).get('run_id'), run)
        tasks.append(dict(id=task['id'], repo=task['repo'], title=task['spec']['title'],
                          state=task['state'], state_label=STATES.get(task['state'], task['state']),
                          run_id=task['run_id'], run_dir=verified.get('run_dir'), original_run_dir=run.get('run_dir'),
                          workspace=verified.get('workspace'), pr_url=run.get('pr_url'),
                          error=run.get('error'), automatic_retry=run.get('automatic_retry'),
                          code_review=verified.get('code_review'), repairs=len(verified.get('repairs', [])), deferred=run.get('deferred'),
                          verification_run_id=verified.get('id')))
        tasks[-1]['ci_repair_status'] = engine.store.control('ci_repair_status:'+task['id'])
        if run.get('pr_url'):
            tasks[-1]['pr_summary'] = review_description(run)
    try:
        service = Service(engine.config.path).status()
    except ApeeError as exc:
        service = dict(error=str(exc), loaded=None, running=None)
    raw = {repo_name(item['repo']): item for item in engine.config.data.get('projects', [])}
    projects = [dict(repo=project['repo'], enabled=project['enabled'], priority=project['priority'],
                     issue_label=project['issue_label'], publish_draft=project['publish_draft'],
                     setup_network=project.get('setup_network', False), allowed_paths=project['allowed_paths'],
                     tests=raw[project['repo']].get('tests', []), setup=raw[project['repo']].get('setup', []),
                     revision=project_revision(raw[project['repo']]))
                for project in sorted(engine.config.projects.values(), key=lambda item: (item['priority'], item['repo']))]
    for project in projects: project['base_branch'] = raw[project['repo']].get('base_branch')
    return dict(version=__version__, at=now(), config=str(engine.config.path),
                state_dir=str(engine.config.state_dir), overview=str(engine.config.state_dir/'overview.md'),
                tasks=tasks, projects=projects, attempts=engine.store.attempts_today(), codex=engine.config.codex,
                daily_attempt_limit=engine.config.daily_attempt_limit,
                worker_active=engine.store.worker_active(), engine_active=engine.store.engine_active(),
                paused=engine.store.control('paused'), codex_hold=engine.store.control('codex_hold'),
                recovery=engine.recovery_status(), auto_recovery=engine.config.auto_recovery, quality=engine.config.quality,
                ci_repair=engine.config.ci_repair,
                planning=engine.config.planning, portfolio=Portfolio(engine).snapshot(),
                notifications=Notifications(engine).snapshot(),
                budget=budget.summary(), budget_history=budget.history(), budget_blocker=budget.blocker(),
                model=engine.config.model, service=service)
