"""Build a private, paused desktop QA instance without touching production state."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sqlite3
import sys
from pathlib import Path

from apee.config import Config
from apee.engine import Engine
from apee.portfolio import Portfolio
from apee.service import atomic_write
from apee.store import now


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', required=True, type=Path)
    parser.add_argument('--from-fixture', type=Path, help='Preserve an earlier private QA fixture while testing a new immutable runtime.')
    args = parser.parse_args()
    root = args.output_root.expanduser().resolve()
    if root.exists(): raise SystemExit('Fixture output already exists.')
    root.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1]
    runtime = root/'runtime'
    shutil.copytree(source/'apee', runtime/'apee', ignore=shutil.ignore_patterns('__pycache__'))
    config = root/'config.json'
    if args.from_fixture:
        previous = args.from_fixture.expanduser().resolve()
        metadata = json.loads((previous/'fixture.json').read_text())
        if metadata['kind'] != 'paused_synthetic_desktop_fixture': raise SystemExit('Source is not a QA fixture.')
        atomic_write(config, (previous/'config.json').read_bytes())
        atomic_write(root/'verify.py', (previous/'verify.py').read_bytes())
        (root/'state').mkdir()
        with sqlite3.connect(str(previous/'state/state.sqlite3')) as old, sqlite3.connect(str(root/'state/state.sqlite3')) as new:
            old.backup(new)
        metadata.update(config=str(config), state=str(root/'state'), copied_from=str(previous),
            source_files={str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in (source/'apee').glob('*') if p.is_file()})
        atomic_write(root/'fixture.json', (json.dumps(metadata, indent=2)+'\n').encode())
        build(root, runtime, config)
        return
    projects = [dict(repo=repo, enabled=True, priority=3, tests=[['python3', '-B', '{config_dir}/verify.py']],
                    allowed_paths=['calculator.py'], publish_draft=False, preserved_option=dict(synthetic=True))
                for repo in ('example/desktop-fixture', 'example/dependency-fixture')]
    atomic_write(config, (json.dumps(dict(version=1, state_dir='state', budget={}, projects=projects), indent=2)+'\n').encode())
    atomic_write(root/'verify.py', b'print("Synthetic UI fixture validation; no model or GitHub calls.")\n')
    engine = Engine(Config(config))
    try:
        engine.store.set_control('paused', dict(at=now(), reason='UI 隔離驗收，禁止開始模型或 GitHub 派工。'))
        portfolio = Portfolio(engine)
        goal = portfolio.save('goals', dict(title='桌面驗收的合成目標', objective='跨專案正確處理算術需求',
            criteria='加法與依賴驗收通過，合成錯誤率不超過 1%', repos=[p['repo'] for p in projects]))
        feedback = portfolio.save('feedback', dict(goal=goal['id'], repo=projects[0]['repo'],
            text='合成回饋：加法在零值輸入時沒有得到正確結果。', source='UI 驗收合成資料'))
        definition = portfolio.save('definitions', dict(goal=goal['id'], repo=projects[0]['repo'],
            name='合成錯誤率', unit='%', direction='lte', target='1'))
        portfolio.save('metrics', dict(definition=definition['id'], value='10', source='UI 驗收合成基準'))
        proposals = portfolio.propose(dict(proposals=[dict(goal=goal['id'], repo=projects[0]['repo'],
            title='合成候選：修復加法邊界', body='修復零值輸入的加法行為。', acceptance='零值、负數、正數與小數驗收通過。',
            sources=[feedback['id']], reason='合成回饋指出具體邊界缺陷。')]), portfolio.context(), 'synthetic-ui-fixture')
        first = engine.enqueue(projects[0]['repo'], '合成前置工作', '驗證正數、負數與零值加法。')
        second = engine.enqueue(projects[1]['repo'], '合成跨專案依賴工作', '前置成果完成後才開始。')
        portfolio.link(dict(task=first, goal=goal['id'], depends_on=[]))
        portfolio.link(dict(task=second, goal=goal['id'], depends_on=[first]))
        evidence = dict(kind='paused_synthetic_desktop_fixture', production_modified=False, actual_model_calls=0,
            config=str(config), state=str(engine.config.state_dir), goal=goal['id'], candidate=proposals[0]['id'],
            source_files={str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in (source/'apee').glob('*') if p.is_file()})
        atomic_write(root/'fixture.json', (json.dumps(evidence, indent=2)+'\n').encode())
    finally: engine.store.close()
    build(root, runtime, config)


def build(root, runtime, config):
    result = subprocess.run([sys.executable, '-B', '-m', 'apee', '--config', str(config), 'desktop', 'build', '--output', str(root/'APEE-Full-QA.app')],
        cwd=runtime, capture_output=True, text=True, timeout=240)
    atomic_write(root/'build.log', (result.stdout+result.stderr).encode())
    if result.returncode: raise SystemExit(result.stdout+result.stderr)
    print(result.stdout)


if __name__=='__main__': main()
