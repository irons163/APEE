import json
import unittest
from pathlib import Path

from apee.budget import Budget
from apee.config import ApeeError, DispatchBlocked
from apee.leases import attempt_lease
from apee.quality import diagnostic_text, validate_review
from test_controls import ControlsFixture, MeteredExecutor, USAGE
from test_engine import RealCommandValidator


class SequenceExecutor(MeteredExecutor):
    def __init__(self, modes=None):
        super().__init__()
        self.modes = modes or ['fix']
        self.specs = []

    def execute(self, workspace, run_dir, spec, project):
        self.mode = self.modes[min(self.calls, len(self.modes)-1)]
        self.specs.append(spec)
        return super().execute(workspace, run_dir, spec, project)


def review_result(approved=True):
    return dict(approved=approved, summary='獨立測試審查', findings=[] if approved else [dict(
        priority=2, title='Synthetic blocking defect', file='calculator.py', line=2,
        body='A structured review fixture requiring another verification round.')])


class Reviewer:
    def __init__(self, results=None, mutate=False):
        self.results = results or [review_result()]
        self.calls = 0
        self.mutate = mutate

    def review(self, workspace, directory, spec, project, base):
        result = self.results[min(self.calls, len(self.results)-1)]
        self.calls += 1
        (directory/'agent-events.log').write_text(json.dumps(dict(type='turn.completed', usage=USAGE))+'\n')
        if self.mutate:
            with (workspace/'calculator.py').open('a') as output: output.write('# unauthorized review edit\n')
        return dict(result)


class QualityTests(ControlsFixture):
    def setUp(self):
        super().setUp()
        self.settings(quality=dict(enabled=True, max_repairs=1))
        self.executor = self.engine.executor = SequenceExecutor()
        self.reviewer = self.engine.reviewer = Reviewer()

    def test_approved_independent_review_binds_tree_and_counts_every_model_call(self):
        task = self.task()
        run = self.engine.execute(task)
        self.assertEqual(self.engine.store.task(task)['state'], 'ready')
        self.assertTrue(run['code_review']['approved'])
        self.assertEqual(run['code_review']['tree'], self.runner.git(Path(run['workspace']), 'rev-parse', run['commit']+'^{tree}'))
        self.assertEqual(Budget(self.config, self.engine.store).summary()['tokens'], 24)
        self.assertEqual(len(self.engine.store.usage_records()), 2)
        self.engine.publish(task)
        self.assertEqual(self.github.creates, 1)

    def test_real_failed_test_repairs_then_runs_tests_and_fresh_review(self):
        self.executor.modes = ['wrong', 'fix']
        run = self.engine.execute(self.task())
        self.assertEqual(len(run['repairs']), 1)
        self.assertFalse(run['verification_rounds'][0]['tests_passed'])
        self.assertTrue(run['verification_rounds'][1]['tests_passed'])
        self.assertIn('修復以下已核對問題', self.executor.specs[1]['body'])
        self.assertEqual(self.reviewer.calls, 1)
        self.assertEqual(Budget(self.config, self.engine.store).summary()['tokens'], 36)
        self.assertTrue((Path(run['run_dir'])/'quality/0/test-0.log').exists())

    def test_blocking_review_repairs_and_is_independently_reviewed_again(self):
        self.reviewer.results = [review_result(False), review_result()]
        run = self.engine.execute(self.task())
        self.assertEqual(self.reviewer.calls, 2)
        self.assertEqual(self.executor.calls, 2)
        self.assertFalse(run['verification_rounds'][0]['review']['approved'])
        self.assertTrue(run['verification_rounds'][1]['review']['approved'])
        self.assertEqual(Budget(self.config, self.engine.store).summary()['tokens'], 48)

    def test_persistent_rejection_hits_repair_limit_and_never_publishes(self):
        self.reviewer.results = [review_result(False)]
        task = self.task()
        with self.assertRaisesRegex(ApeeError, '修復上限'):
            self.engine.execute(task)
        self.assertEqual(self.engine.store.task(task)['state'], 'failed')
        self.assertEqual(self.executor.calls, 2)
        self.assertEqual(self.reviewer.calls, 2)
        self.assertEqual(self.github.creates, 0)
        self.assertEqual(len(self.engine.store.run(task)['verification_rounds']), 2)

    def test_admission_budget_stops_before_review_and_cycle_counts_started_attempt(self):
        self.settings(budget=dict(daily_tokens=12))
        self.github.issue_data['labels'] = []
        task = self.task()
        result = self.engine.cycle()
        self.assertEqual(result['attempts'], 1)
        self.assertEqual(result['blocked']['kind'], 'token_budget')
        self.assertEqual(self.engine.store.task(task)['state'], 'waiting')
        self.assertEqual(self.reviewer.calls, 0)
        self.assertEqual(self.executor.calls, 1)
        self.assertTrue(Path(self.engine.store.run(task)['workspace']).exists())

    def test_budget_blocks_repair_after_recorded_rejection(self):
        self.settings(budget=dict(daily_tokens=24))
        self.reviewer.results = [review_result(False)]
        task = self.task()
        with self.assertRaises(DispatchBlocked): self.engine.execute(task)
        self.assertEqual(self.engine.store.task(task)['state'], 'waiting')
        self.assertEqual(self.executor.calls, 1)
        self.assertEqual(self.reviewer.calls, 1)
        self.assertEqual(len(self.engine.store.run(task)['verification_rounds']), 1)

    def test_review_mutation_and_inconsistent_verdict_do_not_trigger_repair(self):
        self.reviewer.mutate = True
        with self.assertRaisesRegex(ApeeError, '審查改動'):
            self.engine.execute(self.task())
        self.assertEqual(self.executor.calls, 1)
        self.reviewer.mutate = False
        self.reviewer.results = [dict(review_result(False), approved=True)]
        task = self.engine.enqueue('example/demo', 'Contradictory', 'Return 5.')
        with self.assertRaisesRegex(ApeeError, '不一致'): self.engine.execute(task)
        self.assertEqual(self.github.creates, 0)

    def test_test_tampering_is_an_integrity_failure_not_repair_input(self):
        class Tamper(RealCommandValidator):
            def check(inner, workspace, *args, **kwargs):
                result = super().check(workspace, *args, **kwargs)
                if args[2] == 'test':
                    with (workspace/'calculator.py').open('a') as output: output.write('# test changed source\n')
                return result
        self.engine.validator = Tamper(self.runner)
        with self.assertRaisesRegex(ApeeError, '驗證版本不一致'): self.engine.execute(self.task())
        self.assertEqual(self.reviewer.calls, 0)
        self.assertEqual(self.executor.calls, 1)

    def test_publish_requires_review_of_the_actual_committed_tree(self):
        task = self.task()
        run = self.engine.execute(task)
        run['code_review']['tree'] = '0'*40
        self.engine.store.save_run(run)
        with self.assertRaisesRegex(ApeeError, '獨立審查'): self.engine.publish(task)
        self.assertEqual(self.github.pushes, 0)

    def test_phase_reconciliation_and_manual_audit_use_parent_attempt_lease(self):
        run = self.engine.execute(self.task())
        budget = Budget(self.config, self.engine.store)
        folder = Path(run['run_dir'])/'model/review-0'
        key = budget.begin(run, 'review-0', folder)
        with attempt_lease(run['run_dir'], create=False):
            budget.reconcile()
            self.assertEqual(next(r for r in budget.records() if r['run_id']==key)['status'], 'pending')
            with self.assertRaises(ApeeError): budget.record_manual(key, USAGE, '0', 'While active')
        budget.reconcile()
        saved = budget.record_manual(key, USAGE, '0.1', 'Audited review call')
        self.assertEqual(saved['attempt_id'], run['id'])
        self.assertEqual(saved['phase'], 'review-0')
        self.assertEqual(budget.summary()['tokens'], 24)

    def test_review_schema_rejects_escape_paths_and_unverifiable_lines(self):
        run = self.engine.execute(self.task())
        workspace = Path(run['workspace'])
        for path, line in (('../secret', 1), ('calculator.py', 1000), ('.git/config', 1)):
            result = review_result(False)
            result['findings'][0].update(file=path, line=line)
            with self.subTest(path=path), self.assertRaises(ApeeError):
                validate_review(result, workspace, run['base_sha'], self.runner)

    def test_diagnostic_context_redacts_credentials(self):
        result = diagnostic_text('api_key=sk-exampleverylongpassword Bearer ghp_fakecredential password=secretvalue')
        self.assertNotIn('sk-example', result)
        self.assertNotIn('ghp_fake', result)
        self.assertNotIn('secretvalue', result)
