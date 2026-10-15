"""Opt-in real Codex planning acceptance with clearly synthetic evidence."""

import argparse
import hashlib
import json
from pathlib import Path

from apee.budget import Budget
from apee.config import Config
from apee.engine import Engine
from apee.planning import cycle
from apee.portfolio import Portfolio
from apee.service import atomic_write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', type=Path, required=True)
    args = parser.parse_args()
    root = args.output_root.expanduser().resolve()
    if root.exists(): raise SystemExit('Acceptance output exists; use a fresh directory.')
    root.mkdir(parents=True)
    config = root/'config.json'
    atomic_write(config, (json.dumps(dict(version=1, state_dir='state', planning=dict(enabled=True), projects=[dict(
        repo='example/planning-fixture', enabled=True, publish_draft=False,
        tests=[['python3', '-B', '-m', 'unittest']], allowed_paths=['calculator.py', 'test_calculator.py'])]), indent=2)+'\n').encode())
    engine = Engine(Config(config))
    try:
        portfolio = Portfolio(engine)
        goal = portfolio.save('goals', dict(title='Synthetic arithmetic acceptance',
            objective='Make addition correct for positive, negative, zero and decimal values.',
            criteria='The four cases (2,3), (-2,1), (0,5), (0.1,0.2) produce their mathematical sums.',
            repos=['example/planning-fixture'], autonomy='propose'))
        feedback = portfolio.save('feedback', dict(goal=goal['id'], repo='example/planning-fixture',
            text='Synthetic observation: add(2,3) returned -1. Addition currently uses subtraction.', source='Explicit synthetic acceptance fixture'))
        result = cycle(engine)
        assert len(result['proposals'])==1 and not result['enqueued'], result
        candidate = engine.store.document('candidates', result['proposals'][0])
        assert candidate['goal']==goal['id'] and candidate['sources'], candidate
        before = Budget(engine.config, engine.store).summary()
        assert len(before['records'])==1 and not before['unknown_runs'] and before['tokens']>0, before
        selected = portfolio.decide(dict(candidate=candidate['id'], decision='accept', reason='Explicit synthetic acceptance decision'))
        assert len(engine.store.tasks())==1 and portfolio.links()[selected['task']]['goal']==goal['id']
        assert cycle(engine)['proposals']==[] and len(engine.store.model_jobs())==1
        evidence = dict(kind='synthetic_acceptance_fixture', actual_model_call=True,
            synthetic_observations=True, github_writes=0, development_attempts=0,
            source_files={str(path.relative_to(Path(__file__).resolve().parents[1])): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (Path(__file__).resolve().parents[1]/'apee').glob('*.py')},
            goal=goal, feedback=feedback, result=result, candidate=candidate, selected=selected, usage=before,
            outcome=portfolio.measure())
        assert evidence['outcome'][0]['status']=='unknown'
        atomic_write(root/'evidence.json', (json.dumps(evidence, ensure_ascii=False, indent=2)+'\n').encode())
        print(json.dumps(dict(passed=True, evidence=str(root/'evidence.json'), tokens=before['tokens'], model=engine.config.model), ensure_ascii=False))
    finally: engine.store.close()


if __name__=='__main__': main()
