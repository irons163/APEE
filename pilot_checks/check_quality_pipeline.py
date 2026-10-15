"""Explicit real-model acceptance; isolated local Git, no GitHub writes.

Invoke manually with --output-root. This makes at most one repair and one fresh
read-only review using the existing Codex CLI login; it is not a unit-test fake.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from apee.budget import Budget
from apee.config import Config
from apee.engine import Engine
from apee.process import Runner
from apee.store import now


class LocalRepository:
    def __init__(self, seed, runner):
        self.seed, self.runner = seed, runner

    def metadata(self, repo):
        return dict(default_branch='main')

    def clone(self, repo, workspace, base):
        self.runner.run(['git', 'clone', '--no-local', str(self.seed), str(workspace)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', type=Path, required=True)
    args = parser.parse_args()
    root = args.output_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    runner = Runner()
    seed = root/'seed'
    seed.mkdir()
    runner.git(seed, 'init', '-b', 'main')
    (seed/'calculator.py').write_text('def add(a, b):\n    return a - b\n')
    (seed/'.gitignore').write_text('__pycache__/\n')
    runner.git(seed, 'add', '--all')
    runner.git(seed, '-c', 'user.name=Acceptance', '-c', 'user.email=acceptance@localhost',
               'commit', '-m', 'Seed arithmetic regression')
    (seed/'calculator.py').write_text('def add(a, b):\n    return a * b\n')
    patch = root/'failed-candidate.patch'
    runner.git(seed, 'diff', '--binary', log=patch)
    runner.git(seed, 'restore', 'calculator.py')
    check = root/'acceptance.py'
    check.write_text('import sys\nsys.path.insert(0, ".")\nfrom calculator import add\n'
                     'for a,b in [(2,3),(-5,2),(0,0),(1.5,2.25)]:\n'
                     '    assert add(a,b)==a+b, (a,b,add(a,b))\n')
    config_path = root/'config.json'
    config_path.write_text(json.dumps(dict(version=1, state_dir='state', agent_timeout_seconds=240,
        command_timeout_seconds=30, quality=dict(enabled=True, max_repairs=1), projects=[dict(
            repo='example/quality-acceptance', enabled=True, publish_draft=False,
            tests=[[sys.executable, '-B', str(check)]], allowed_paths=['calculator.py'])])))
    config_path.chmod(0o600)
    source = Path(__file__).resolve().parents[1]
    fingerprints = {str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((source/'apee').glob('*.py'))}
    evidence = dict(at=now(), source_files=fingerprints, provenance='real Codex repair and independent review; local Git adapter',
                    github_writes=0, expected_model_calls=2, status='running')
    proof = root/'evidence.json'
    proof.write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n')
    engine = Engine(Config(config_path), runner, LocalRepository(seed, runner))
    try:
        task = engine.enqueue('example/quality-acceptance', 'Fix addition with independent review',
                              'add(a,b) must return a+b for positive, negative, zero and fractional inputs. '
                              'Preserve acceptance checks; change only calculator.py.')
        run = engine.execute(task, local_only=True, patch=patch)
        records = engine.store.usage_records()
        assert engine.store.task(task)['state']=='ready'
        assert len(run['repairs'])==1 and run['repairs'][0]['agent']['backend']=='codex'
        assert run['code_review']['approved'] and run['code_review']['backend']=='codex'
        assert len(records)==2 and all(item['status']=='known' for item in records)
        assert {item['phase'] for item in records}=={'repair-1','review-1'}
        assert not run['verification_rounds'][0]['tests_passed']
        assert run['verification_rounds'][1]['tests_passed']
        assert run['code_review']['tree']==runner.git(Path(run['workspace']), 'rev-parse', run['commit']+'^{tree}')
        evidence.update(status='passed', completed_at=now(), task=task, run=run,
                        usage=Budget(engine.config, engine.store).summary())
    except BaseException as exc:
        evidence.update(status='failed', completed_at=now(), error=str(exc), records=engine.store.usage_records())
        raise
    finally:
        proof.write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n')
        engine.store.close()
    print(json.dumps(dict(status=evidence['status'], proof=str(proof), tokens=evidence['usage']['tokens'])))


if __name__=='__main__':
    main()
