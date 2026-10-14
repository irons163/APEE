"""A single local snapshot and validated settings edits shared with the desktop."""

import json
import tempfile
from pathlib import Path

from . import __version__
from .budget import Budget
from .config import ApeeError, Config, repo_name
from .report import STATES, review_description
from .service import Service, atomic_write
from .store import now


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
    if not isinstance(changes, dict) or set(changes) - {'model', 'budget', 'auto_recovery'}:
        raise ApeeError('只接受 model、budget 與 auto_recovery 設定。')
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


def snapshot(engine):
    budget = Budget(engine.config, engine.store)
    tasks = []
    for task in engine.store.tasks():
        run = engine.store.run(task['id']) if task['run_id'] else {}
        tasks.append(dict(id=task['id'], repo=task['repo'], title=task['spec']['title'],
                          state=task['state'], state_label=STATES.get(task['state'], task['state']),
                          run_id=task['run_id'], run_dir=run.get('run_dir'),
                          workspace=run.get('workspace'), pr_url=run.get('pr_url'),
                          error=run.get('error'), automatic_retry=run.get('automatic_retry')))
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
                     tests=raw[project['repo']].get('tests', []), setup=raw[project['repo']].get('setup', []))
                for project in sorted(engine.config.projects.values(), key=lambda item: (item['priority'], item['repo']))]
    return dict(version=__version__, at=now(), config=str(engine.config.path),
                state_dir=str(engine.config.state_dir), overview=str(engine.config.state_dir/'overview.md'),
                tasks=tasks, projects=projects, attempts=engine.store.attempts_today(),
                daily_attempt_limit=engine.config.daily_attempt_limit,
                worker_active=engine.store.worker_active(), engine_active=engine.store.engine_active(),
                paused=engine.store.control('paused'), codex_hold=engine.store.control('codex_hold'),
                recovery=engine.recovery_status(), auto_recovery=engine.config.auto_recovery,
                budget=budget.summary(), budget_history=budget.history(), budget_blocker=budget.blocker(),
                model=engine.config.model, service=service)
