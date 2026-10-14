import json
import unittest
from pathlib import Path
from unittest.mock import patch

from apee.budget import Budget
from apee.config import ApeeError, DispatchBlocked
from apee.leases import attempt_lease
from apee.planning import cycle
from apee.portfolio import Portfolio
from apee.store import Store
from test_controls import ControlsFixture, USAGE


class Planner:
    def __init__(self):
        self.calls = 0
        self.bad_source = False
    def plan(self, directory, context, limit):
        self.calls += 1
        (directory/'agent-events.log').write_text(json.dumps(dict(type='turn.completed', usage=USAGE))+'\n')
        goal = context['goals'][0]
        return dict(proposals=[dict(goal=goal['id'], repo=goal['repos'][0], title='Resolve observed addition defect',
            body='Implement correct addition based on the observed problem.', acceptance='2+3=5 and negative arguments work.',
            sources=['fabricated-source' if self.bad_source else context['feedback'][0]['id']], reason='Observed problem blocks the stated goal.')])


class PortfolioTests(ControlsFixture):
    def setUp(self):
        super().setUp()
        self.settings(planning=dict(enabled=True, max_active_projects=1, max_new_tasks_per_cycle=1,
            max_pending_candidates=2, max_calls_per_day=5))
        self.portfolio = Portfolio(self.engine)
        self.goal = self.portfolio.save('goals', dict(title='Correct arithmetic', objective='Reliable arithmetic across projects',
            criteria='Addition acceptance tests and defect rate meet target', repos=['example/demo'], priority=1))
        self.feedback = self.portfolio.save('feedback', dict(goal=self.goal['id'], repo='example/demo',
            text='Observed wrong addition results', source='Synthetic user report', observed_at='2026-10-12T08:00:00+08:00'))
        self.engine.planner = Planner()

    def test_goal_evidence_and_links_survive_restart_and_goal_edits_are_guarded(self):
        task = self.task()
        self.portfolio.link(dict(task=task, goal=self.goal['id'], depends_on=[]))
        other = Store(self.config.state_dir)
        try:
            self.assertEqual(other.document('goals', self.goal['id'])['objective'], self.goal['objective'])
            self.assertEqual(other.document('feedback', self.feedback['id'])['observed_at'], '2026-10-12T00:00:00+00:00')
            self.assertEqual(other.document('task_context', task)['goal'], self.goal['id'])
        finally: other.close()
        with self.assertRaisesRegex(ApeeError, '已改變'):
            self.portfolio.save('goals', {k:v for k,v in self.goal.items() if k not in ('created', 'updated')})

    def test_dependency_cycle_is_rejected_and_unverified_delivery_remains_blocked(self):
        a = self.task()
        b = self.engine.enqueue('example/demo', 'Second', 'Depend on first accepted output.')
        self.portfolio.link(dict(task=b, goal=self.goal['id'], depends_on=[a]))
        with self.assertRaisesRegex(ApeeError, '循環'):
            self.portfolio.link(dict(task=a, goal=self.goal['id'], depends_on=[b]))
        with self.assertRaisesRegex(ApeeError, '等待依賴'):
            self.engine.execute(b)
        run = self.engine.execute(a)
        self.assertEqual(self.engine.next_task(), None)
        self.portfolio.complete_local(dict(task=a, reason='Synthetic accepted test output'))
        self.assertEqual(self.engine.next_task(), b)
        result = self.engine.execute(b)
        self.assertEqual(result['goal_context']['depends_on'], [a])
        self.assertEqual(self.engine.store.run(a)['commit'], run['commit'])

    def test_project_concurrency_cap_allows_same_project_and_releases_after_closed_delivery(self):
        data = dict(self.data['projects'][0], repo='example/other', priority=1)
        self.data['projects'].append(data)
        self.settings()
        self.portfolio = Portfolio(self.engine)
        first = self.task()
        run = self.engine.execute(first)
        other = self.engine.enqueue('example/other', 'Other project', 'Independent arithmetic acceptance.')
        same = self.engine.enqueue('example/demo', 'Same project', 'Continue same project while another delivery awaits review.')
        decision = self.portfolio.decisions()
        self.assertEqual(decision['active_projects'], ['example/demo'])
        self.assertEqual(decision['next_task'], same)
        with self.assertRaisesRegex(ApeeError, '同時推進'): self.engine.execute(other)
        self.engine.store.transition(first, 'closed', run=run)
        self.assertEqual(self.engine.next_task(), other)

    def test_accepted_local_completion_releases_project_capacity_for_cross_project_dependency(self):
        self.data['projects'].append(dict(self.data['projects'][0], repo='example/other'))
        self.settings()
        self.portfolio = Portfolio(self.engine)
        first = self.task()
        other = self.engine.enqueue('example/other', 'Dependent project', 'Use accepted predecessor output.')
        self.portfolio.link(dict(task=other, depends_on=[first]))
        self.engine.execute(first)
        self.assertIsNone(self.engine.next_task())
        self.portfolio.complete_local(dict(task=first, reason='Accepted local boundary test'))
        self.assertEqual(self.engine.next_task(), other)

    def test_proposals_require_real_source_ids_and_all_or_nothing_validation(self):
        self.engine.planner.bad_source = True
        result = cycle(self.engine)
        self.assertIn('來源無法核對', result['error'])
        self.assertEqual(self.engine.store.documents('candidates'), [])
        self.assertEqual(len(self.engine.store.model_jobs()), 1)
        self.assertEqual(Budget(self.config, self.engine.store).summary()['tokens'], 12)

    def test_planning_is_metered_deduplicated_and_propose_policy_waits_for_user(self):
        result = cycle(self.engine)
        self.assertEqual(len(result['proposals']), 1)
        self.assertEqual(result['enqueued'], [])
        self.assertEqual(self.engine.store.tasks(), [])
        candidate = self.engine.store.documents('candidates')[0]
        selected = self.portfolio.decide(dict(candidate=candidate['id'], decision='accept', reason='User accepts observed need'))
        again = self.portfolio.decide(dict(candidate=candidate['id'], decision='accept', reason='Repeated click'))
        self.assertEqual(selected['task'], again['task'])
        self.assertEqual(len(self.engine.store.tasks()), 1)
        self.assertEqual(cycle(self.engine)['proposals'], [])
        self.assertEqual(self.engine.planner.calls, 1)
        self.assertEqual(Budget(self.config, self.engine.store).summary()['tokens'], 12)
        self.assertEqual(self.engine.store.total_attempts(), 0)

    def test_explicit_goal_autonomy_enqueues_bounded_task_with_saved_reason(self):
        data = {k:v for k,v in self.goal.items() if k not in ('created', 'updated')}
        self.portfolio.save('goals', dict(data, expected_updated=self.goal['updated'], autonomy='enqueue'))
        result = cycle(self.engine)
        self.assertEqual(len(result['enqueued']), 1)
        task = self.engine.store.task(result['enqueued'][0])
        self.assertEqual(task['spec']['source'], 'proposal')
        self.assertEqual(task['spec']['sources'], [self.feedback['id']])
        self.assertEqual(cycle(self.engine)['enqueued'], [])
        self.assertEqual(self.engine.planner.calls, 1)

    def test_explicit_single_planning_call_respects_budgets_without_enabling_background_planning(self):
        self.settings(planning=dict(enabled=False))
        self.assertEqual(cycle(self.engine)['proposals'], [])
        self.assertEqual(self.engine.planner.calls, 0)
        self.assertEqual(len(cycle(self.engine, requested=True)['proposals']), 1)
        self.assertFalse(self.engine.config.planning['enabled'])

    def test_modified_local_checkout_cannot_release_dependencies(self):
        task = self.task()
        run = self.engine.execute(task)
        (Path(run['workspace'])/'calculator.py').write_text('changed after validation\n')
        with self.assertRaisesRegex(ApeeError, '已變更'):
            self.portfolio.complete_local(dict(task=task, reason='Attempted stale acceptance'))
        self.assertFalse(self.portfolio.completion(task))

    def test_pause_budget_unknown_usage_and_daily_planning_cap_prevent_calls(self):
        self.engine.store.set_control('paused', dict(reason='User pause'))
        self.assertEqual(cycle(self.engine)['blocked']['kind'], 'paused')
        self.engine.store.set_control('paused', None)
        self.settings(planning=dict(enabled=True, max_calls_per_day=1), budget=dict(daily_tokens=12))
        self.portfolio = Portfolio(self.engine)
        cycle(self.engine)
        candidate = self.engine.store.documents('candidates')[0]
        self.portfolio.decide(dict(candidate=candidate['id'], decision='reject', reason='No longer needed'))
        self.assertEqual(cycle(self.engine)['blocked']['kind'], 'token_budget')
        self.settings(budget={})
        self.assertEqual(cycle(self.engine)['blocked']['kind'], 'planning_daily_limit')
        self.assertEqual(self.engine.planner.calls, 1)

    def test_planning_lease_blocks_development_and_audit_and_reconciles_after_crash(self):
        root = self.config.state_dir/'model-jobs'/'orphan'
        root.mkdir(parents=True)
        job = dict(id='orphan', run_dir=str(root), state='running', started_at='2026-10-13T00:00:00+00:00', execution_backend='codex', lease_version=1)
        self.engine.store.save_model_job(job)
        key = Budget(self.config, self.engine.store).begin(job, 'planning', root)
        with attempt_lease(root):
            with self.assertRaises(DispatchBlocked): self.engine.execute(self.task())
            with self.assertRaises(ApeeError): Budget(self.config, self.engine.store).record_manual(key, USAGE, None, 'Still active')
            Budget(self.config, self.engine.store).reconcile()
            self.assertEqual(self.engine.store.usage_records()[0]['status'], 'pending')
        Budget(self.config, self.engine.store).reconcile()
        self.assertEqual(self.engine.store.usage_records()[0]['status'], 'unknown')
        cycle(self.engine)
        self.assertEqual(next(j for j in self.engine.store.model_jobs() if j['id']=='orphan')['state'], 'interrupted')
        audited = Budget(self.config, self.engine.store).record_manual(key, USAGE, None, 'Synthetic recovered usage')
        self.assertEqual(audited['usage']['total_tokens'], 12)

    def test_product_success_requires_post_delivery_evidence_not_only_passed_tests(self):
        definition = self.portfolio.save('definitions', dict(goal=self.goal['id'], repo='example/demo', name='Defect rate',
            unit='percent', direction='lte', target='1'))
        self.portfolio.save('metrics', dict(definition=definition['id'], value='10', source='Synthetic baseline', observed_at='2026-10-12T00:00:00Z'))
        candidate = cycle(self.engine)['proposals'][0]
        selected = self.portfolio.decide(dict(candidate=candidate, decision='accept', reason='Synthetic measurement test'))
        self.engine.execute(selected['task'])
        self.assertEqual(self.portfolio.measure()[0]['status'], 'unknown')
        with patch('apee.portfolio.now', return_value='2026-10-14T00:00:00+00:00'):
            self.portfolio.complete_local(dict(task=selected['task'], reason='Accepted synthetic delivery'))
            self.assertEqual(self.portfolio.measure()[0]['status'], 'unknown')
        with patch('apee.portfolio.now', return_value='2026-10-15T00:01:00+00:00'):
            after = self.portfolio.save('metrics', dict(definition=definition['id'], value='0.5', source='Synthetic post-delivery sample', observed_at='2026-10-15T00:00:00Z'))
            measured = self.portfolio.measure()[0]
        self.assertEqual(measured['status'], 'confirmed')
        self.assertEqual(measured['comparisons'][0]['after'], after['id'])
        with patch('apee.portfolio.now', return_value='2026-10-16T00:01:00+00:00'):
            self.portfolio.save('metrics', dict(definition=definition['id'], value='15', source='Synthetic regression sample', observed_at='2026-10-16T00:00:00Z'))
            self.assertEqual(self.portfolio.measure()[0]['status'], 'regressed')

    def test_future_observations_are_not_accepted_as_product_evidence(self):
        definition = self.portfolio.save('definitions', dict(goal=self.goal['id'], repo='example/demo', name='Future sample', unit='count', direction='lte', target='1'))
        with self.assertRaisesRegex(ApeeError, '不能在未來'):
            self.portfolio.save('metrics', dict(definition=definition['id'], value='0', source='Unobserved prediction', observed_at='2099-01-01T00:00:00Z'))
        self.assertEqual(self.portfolio.latest_metrics(), {})

    def test_definition_changes_nonfinite_numbers_and_foreign_goals_are_rejected(self):
        for bad in ('NaN', 'Infinity', '1e9', 1, '-inf'):
            with self.subTest(bad=bad), self.assertRaises(ApeeError):
                self.portfolio.save('definitions', dict(goal=self.goal['id'], repo='example/demo', name='Rate', unit='percent', direction='lte', target=bad))
        with self.assertRaises(ApeeError):
            self.portfolio.save('feedback', dict(goal=self.goal['id'], repo='example/other', text='Foreign', source='fixture'))
        with self.assertRaises(ApeeError):
            self.portfolio.save('feedback', dict(goal=self.goal['id'], repo='example/demo', text='Missing timezone', source='fixture', observed_at='2026-10-12'))

    def test_goal_priority_and_dependency_reasons_drive_persistent_dispatch_context(self):
        unlinked = self.task()
        linked = self.engine.enqueue('example/demo', 'Higher goal priority', 'Goal-related acceptance.')
        self.portfolio.link(dict(task=linked, goal=self.goal['id'], depends_on=[]))
        self.assertEqual(self.engine.next_task(), linked)
        run = self.engine.execute(linked)
        self.assertEqual(self.engine.store.control('dispatch_decision')['task'], linked)
        self.assertEqual(run['goal_context']['goal']['criteria'], self.goal['criteria'])
        self.assertEqual(self.engine.store.task(unlinked)['state'], 'queued')
