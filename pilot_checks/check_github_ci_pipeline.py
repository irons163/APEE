"""Opt-in actual GitHub Actions/Codex CI repair, isolated from the repository default branch."""

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

from apee import ci
from apee.budget import Budget
from apee.config import Config
from apee.engine import Engine
from apee.github import GitHub
from apee.process import Runner
from apee.service import atomic_write


def wait_ci(engine, task, expected, timeout=300):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        engine.refresh_reviews(task)
        run = engine.store.run(task)
        status = run.get('pr_status', {})
        if not run.get('pr_refresh_error') and status.get('ci')==expected and status['matches_verified_commit']:
            return status
        if status.get('ci')=='failed' and expected=='passed':
            raise RuntimeError('Actual CI still failed after repair: '+json.dumps(status))
        time.sleep(5)
    raise RuntimeError('Timed out waiting for actual GitHub CI '+expected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', type=Path, required=True)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--bootstrap-remote', help='An already configured repository SSH remote, used only to seed the isolated workflow.')
    args = parser.parse_args()
    root = args.output_root.expanduser().resolve()
    if root.exists(): raise SystemExit('Output exists; choose a fresh directory.')
    root.mkdir(parents=True)
    runner = Runner()
    github = GitHub(runner)
    meta = github.metadata(args.repo)
    assert not meta['archived']
    checkout_sha = github.json(['api', '--hostname', 'github.com', 'repos/actions/checkout/git/ref/tags/v4.2.2'])['object']['sha']
    assert len(checkout_sha)==40
    base = 'codex/ci-acceptance-base-'+uuid.uuid4().hex[:12]
    source = root/'baseline'
    source.mkdir()
    runner.git(source, 'init', '-b', base)
    files = {
        '.gitignore': '__pycache__/\n',
        'calculator.py': 'def add(a, b):\n    return a + b\n',
        'test_local.py': 'import unittest\nfrom calculator import add\nclass Local(unittest.TestCase):\n    def test_positive(self): self.assertEqual(add(2, 3), 5)\n    def test_negative(self): self.assertEqual(add(-2, 1), -1)\n',
        'test_ci.py': 'import unittest\nfrom calculator import add\nclass CI(unittest.TestCase):\n    def test_zero(self): self.assertEqual(add(0, 5), 5)\n    def test_decimal(self): self.assertAlmostEqual(add(0.1, 0.2), 0.3)\n',
        '.github/workflows/acceptance.yml': 'name: APEE synthetic CI acceptance\non:\n  pull_request:\n    branches: ["'+base+'"]\npermissions:\n  contents: read\njobs:\n  arithmetic:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@'+checkout_sha+'\n        with:\n          persist-credentials: false\n      - run: python3 -B -m unittest -v test_local test_ci\n',
    }
    for name, contents in files.items(): atomic_write(source/name, contents.encode())
    runner.git(source, 'add', '--all')
    runner.git(source, '-c', 'user.name=APEE acceptance', '-c', 'user.email=apee@localhost', 'commit', '-m', 'Isolated synthetic CI acceptance baseline; do not merge')
    baseline = runner.git(source, 'rev-parse', 'HEAD')
    if args.bootstrap_remote:
        if not args.bootstrap_remote.lower().endswith(':'+args.repo.lower()+'.git'):
            raise SystemExit('Bootstrap SSH remote must identify the same repository.')
        if runner.git(source, 'ls-remote', args.bootstrap_remote, 'refs/heads/'+base):
            raise SystemExit('Bootstrap branch unexpectedly exists.')
        runner.git(source, 'push', '--', args.bootstrap_remote, baseline+':refs/heads/'+base, timeout=300)
    else:
        github.push(args.repo, source, base, baseline)
    (source/'calculator.py').write_text('def add(a, b):\n    if a == 0:\n        return None\n    return a + b\n')
    patch = root/'synthetic-failing.patch'
    runner.git(source, 'diff', '--binary', log=patch)
    config = root/'config.json'
    atomic_write(config, (json.dumps(dict(version=1, state_dir='state', ci_repair=dict(enabled=True, max_attempts_per_pr=1),
        quality=dict(enabled=False, max_repairs=1), projects=[dict(repo=args.repo, enabled=True, publish_draft=True,
            base_branch=base, allowed_paths=['calculator.py'], tests=[[sys.executable, '-B', '-m', 'unittest', '-v', 'test_local']])]), indent=2)+'\n').encode())
    engine = Engine(Config(config))
    try:
        task = engine.enqueue(args.repo, '[Acceptance only; do not merge] APEE bounded CI repair',
            'Synthetic acceptance fixture. Addition must work for positive, negative, zero and decimal inputs. '
            'Keep all tests and workflow checks intact. This isolated PR must never be merged into the product.')
        initial = engine.execute(task, patch=patch)
        atomic_write(root/'started.json', (json.dumps(dict(pr_url=initial['pr_url'], task=task, base=base, baseline=baseline,
            default_branch=meta['default_branch'], original_commit=initial['commit'], kind='synthetic_actual_github_acceptance'), indent=2)+'\n').encode())
        print(json.dumps(dict(stage='pr_created', url=initial['pr_url']), ensure_ascii=False), flush=True)
        failed = wait_ci(engine, task, 'failed')
        scheduled = ci.schedule(engine)
        assert len(scheduled['added'])==1 and not scheduled['errors'], scheduled
        child = scheduled['added'][0]
        repaired = engine.execute(child)
        assert repaired['quality_required'] and repaired['code_review']['approved'] and repaired['pr_url']==initial['pr_url']
        passed = wait_ci(engine, task, 'passed')
        assert engine.store.run(task)['commit']==initial['commit']
        assert passed['verified_commit']==repaired['commit'] and passed['verification_run']==repaired['id']
        assert ci.schedule(engine)['added']==[]
        usage = Budget(engine.config, engine.store).summary()
        assert len(usage['records'])>=2 and not usage['unknown_runs'] and usage['tokens']>0
        evidence = dict(kind='synthetic_actual_github_acceptance', passed=True, default_branch_unchanged=meta['default_branch'],
            base=base, baseline=baseline, pr_url=initial['pr_url'], task=task, child=child, original_commit=initial['commit'],
            repaired_commit=repaired['commit'], failed=failed, passed_ci=passed, quality=repaired['code_review'],
            logs=engine.store.task(child)['spec']['ci_logs'], usage=usage,
            production_modified=False, created_prs=1, branches_updated=[base, initial['branch']])
        atomic_write(root/'evidence.json', (json.dumps(evidence, ensure_ascii=False, indent=2)+'\n').encode())
        print(json.dumps(dict(passed=True, pr_url=initial['pr_url'], evidence=str(root/'evidence.json'), tokens=usage['tokens']), ensure_ascii=False), flush=True)
    finally: engine.store.close()


if __name__=='__main__': main()
