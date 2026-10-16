import io
import json
import os
import plistlib
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from apee import __version__
from apee.budget import Budget, estimate, log_usage, token_usage
from apee.cli import main
from apee.config import ApeeError, Config, DispatchBlocked
from apee.controls import add_project, configure, snapshot
from apee.desktop import build
from apee.engine import Engine
from apee.leases import attempt_lease
from apee.worker import work
from test_engine import EngineFixture, FixExecutor, RealCommandValidator
from test_recovery import CrashHarness, wait_until


USAGE = dict(input_tokens=10, cached_input_tokens=4, output_tokens=2)
PRICES = dict(input='2', cached_input='1', output='3')  # Fictional test rates.


class MeteredExecutor(FixExecutor):
    def execute(self, workspace, run_dir, spec, project):
        (run_dir/'agent-events.log').write_text(json.dumps(dict(type='turn.completed', usage=USAGE))+'\n')
        return dict(super().execute(workspace, run_dir, spec, project), backend='codex', usage=[USAGE])


class ControlsFixture(EngineFixture):
    def settings(self, **changes):
        self.data.update(changes)
        self.path.write_text(json.dumps(self.data))
        self.engine.reload_config()
        self.config = self.engine.config

    def abandoned(self, title='Abandoned'):
        task = self.engine.enqueue('example/demo', title, 'Return 5.')
        self.executor.mode = 'wrong'
        with self.assertRaises(ApeeError):
            self.engine.execute(task)
        run = self.engine.store.run(task)
        self.engine.store.transition(task, 'running', run=run)
        self.executor.mode = 'fix'
        return task, run

    def policy(self, **values):
        self.settings(auto_recovery=dict(dict(enabled=True, cooldown_seconds=60, max_retries=1, max_per_cycle=1), **values))

    def recover_once(self):
        self.engine.auto_recover(at='2026-10-13T00:00:00+00:00')
        return self.engine.auto_recover(at='2026-10-13T00:01:00+00:00')


class AutomaticRecoveryTests(CrashHarness, ControlsFixture):
    def test_real_crash_waits_for_child_then_retries_fresh_checkout_once(self):
        self.policy()
        task = self.task()
        process = self.start_controller(task)
        child, run_dir = None, None
        try:
            child = wait_until(lambda: self.child_ready(task), 'child did not start')
            old = self.engine.store.run(task)
            run_dir = Path(old['run_dir'])
            process.kill()
            process.wait(timeout=5)
            self.assertEqual(self.engine.auto_recover(), [])
            self.assertIsNone(self.engine.store.control('recovery_observations'))
            self.stop_child(child, run_dir)
            self.assertEqual(self.engine.auto_recover(at='2026-10-13T00:00:00+00:00'), [])
            self.assertEqual(self.engine.auto_recover(at='2026-10-13T00:00:59+00:00'), [])
            result = self.engine.auto_recover(at='2026-10-13T00:01:00+00:00')
            self.assertTrue(result[0]['retried'])
            self.assertEqual(self.engine.store.task(task)['state'], 'queued')
            saved = self.engine.store.run(task)
            self.assertEqual(saved['recovery']['source'], 'automatic')
            self.assertEqual(saved['recovery']['observation']['idle_since'], '2026-10-13T00:00:00+00:00')
            events = self.event_count()
            self.assertEqual(self.engine.auto_recover(), [])
            self.assertEqual(self.event_count(), events)
            fresh = self.engine.execute(task, local_only=True)
            self.assertNotEqual(fresh['workspace'], old['workspace'])
            self.assertTrue((run_dir/'repo/partial-work.txt').exists())
            self.assertEqual(self.engine.store.total_attempts(), 2)
            self.assertEqual(self.executor.calls, 1)
        finally:
            self.cleanup_controller(process, child, run_dir)

    def test_default_policy_requires_manual_recovery(self):
        task, _ = self.abandoned()
        self.assertEqual(self.recover_once(), [])
        self.assertEqual(self.engine.store.task(task)['state'], 'running')

    def test_retry_limit_is_persistent_and_stops_repeated_crashes(self):
        self.policy()
        task, _ = self.abandoned()
        self.assertTrue(self.recover_once()[0]['retried'])
        self.executor.mode = 'wrong'
        with self.assertRaises(ApeeError):
            self.engine.execute(task)
        run = self.engine.store.run(task)
        self.engine.store.transition(task, 'testing', run=run)
        self.engine.store.close()
        self.engine = Engine(Config(self.path), self.runner, self.github, self.executor, RealCommandValidator(self.runner))
        result = self.recover_once()
        self.assertFalse(result[0]['retried'])
        self.assertEqual(self.engine.store.task(task)['state'], 'interrupted')
        self.assertEqual(self.engine.store.total_attempts(), 2)
        self.assertEqual(self.engine.auto_recover(), [])

    def test_pause_resets_observation_and_does_not_change_task(self):
        self.policy()
        task, _ = self.abandoned()
        self.engine.auto_recover(at='2026-10-13T00:00:00+00:00')
        self.engine.store.set_control('paused', dict(reason='human'))
        self.assertEqual(self.engine.auto_recover(at='2026-10-13T00:10:00+00:00'), [])
        self.assertIsNone(self.engine.store.control('recovery_observations'))
        self.assertEqual(self.engine.store.task(task)['state'], 'running')
        self.engine.store.set_control('paused', None)
        self.assertEqual(self.engine.auto_recover(at='2026-10-13T00:10:00+00:00'), [])

    def test_provider_hold_defers_retry_until_explicit_resume(self):
        self.policy()
        task, _ = self.abandoned()
        self.engine.store.set_control('codex_hold', dict(kind='usage_limit', message='hold'))
        self.assertFalse(self.recover_once()[0]['retried'])
        self.assertEqual(self.engine.store.task(task)['state'], 'interrupted')
        self.engine.store.resume_codex()
        self.assertTrue(self.engine.auto_recover()[0]['retried'])

    def test_daily_limit_defers_retry_and_attempt_is_counted_on_execution(self):
        self.policy()
        with patch('apee.store.now', return_value='2000-01-01T00:01:00+00:00'):
            task, _ = self.abandoned()
            self.settings(daily_attempt_limit=1)
            self.assertFalse(self.recover_once()[0]['retried'])
            self.assertEqual(self.engine.store.total_attempts(), 1)
        with patch('apee.store.now', return_value='2000-01-02T00:01:00+00:00'):
            self.assertTrue(self.engine.auto_recover()[0]['retried'])
            self.assertEqual(self.engine.store.total_attempts(), 1)
            self.engine.execute(task)
        self.assertEqual(self.engine.store.total_attempts(), 2)

    def test_unknown_usage_budget_prevents_automatic_retry(self):
        self.policy()
        task, _ = self.abandoned()
        self.settings(budget=dict(daily_tokens=100))
        self.assertFalse(self.recover_once()[0]['retried'])
        self.assertEqual(self.engine.store.task(task)['state'], 'interrupted')
        self.assertEqual(Budget(self.config, self.engine.store).blocker()['kind'], 'usage_unknown')

    def test_legacy_patch_missing_or_unreadable_leases_need_manual_attention(self):
        self.policy()
        for mode in ('legacy', 'patch', 'missing', 'unreadable', 'publishing', 'ready'):
            with self.subTest(mode=mode):
                task, run = self.abandoned(mode)
                if mode == 'legacy':
                    run.pop('execution_backend')
                elif mode == 'patch':
                    run['execution_backend'] = 'imported_patch'
                elif mode in ('missing', 'unreadable'):
                    lock = Path(run['run_dir'])/'active.lock'
                    lock.unlink()
                    if mode == 'unreadable':
                        lock.mkdir()
                state = mode if mode in ('publishing', 'ready') else 'running'
                self.engine.store.transition(task, state, run=run)
                self.assertEqual(self.recover_once(), [])
                self.assertEqual(self.engine.store.task(task)['state'], state)
                # Remove this synthetic blocker so another task can be prepared.
                if mode == 'unreadable':
                    (Path(run['run_dir'])/'active.lock').rmdir()
                self.engine.store.transition(task, 'failed')

    def test_disabled_project_is_not_automatically_recovered(self):
        self.policy()
        task, _ = self.abandoned()
        self.data['projects'][0]['enabled'] = False
        self.settings()
        self.assertEqual(self.recover_once(), [])
        self.assertEqual(self.engine.store.task(task)['state'], 'running')

    def test_reacquired_lease_and_clock_reversal_restart_observation(self):
        self.policy()
        task, run = self.abandoned()
        self.engine.auto_recover(at='2026-10-13T00:10:00+00:00')
        self.assertEqual(self.engine.auto_recover(at='2026-10-13T00:09:00+00:00'), [])
        with attempt_lease(run['run_dir'], create=False):
            self.assertEqual(self.engine.auto_recover(at='2026-10-13T00:11:00+00:00'), [])
        self.assertEqual(self.engine.auto_recover(at='2026-10-13T00:12:00+00:00'), [])
        self.assertEqual(self.engine.store.task(task)['state'], 'running')

    def test_one_recovery_per_cycle_and_worker_attempt_cap(self):
        self.policy()
        first, first_run = self.abandoned('First')
        self.engine.store.transition(first, 'failed')
        second, _ = self.abandoned('Second')
        self.engine.store.transition(first, 'running', run=first_run)
        first, second = [task['id'] for task in self.engine.store.tasks()]
        self.engine.auto_recover(at='2026-10-13T00:00:00+00:00')
        self.assertEqual(len(self.engine.auto_recover(at='2026-10-13T00:01:00+00:00')), 1)
        self.assertEqual(self.engine.store.task(first)['state'], 'queued')
        self.assertEqual(self.engine.store.task(second)['state'], 'running')
        self.assertEqual(len(self.engine.auto_recover(at='2026-10-13T00:01:01+00:00')), 1)
        self.github.issues = lambda project: []
        status = work(self.engine, max_cycles=1, max_tasks=1, local_only=True)
        self.assertEqual(status['attempts'], 1)
        self.assertEqual(self.engine.store.task(first)['state'], 'ready')
        self.assertEqual(self.engine.store.task(second)['state'], 'queued')

    def test_zero_retry_policy_marks_interrupted_without_starting_development(self):
        self.policy(max_retries=0)
        task, _ = self.abandoned()
        calls=self.executor.calls
        self.assertFalse(self.recover_once()[0]['retried'])
        self.assertEqual(self.engine.store.task(task)['state'],'interrupted')
        self.assertEqual(self.executor.calls,calls)

    def test_historical_live_lease_blocks_automatic_recovery_of_later_attempt(self):
        self.policy()
        task, old = self.abandoned()
        self.engine.recover(task)
        self.engine.retry(task)
        self.executor.mode='wrong'
        with self.assertRaises(ApeeError):
            self.engine.execute(task)
        current=self.engine.store.run(task)
        self.engine.store.transition(task,'running',run=current)
        with attempt_lease(old['run_dir'],create=False):
            self.assertEqual(self.recover_once(),[])
        self.assertEqual(self.engine.store.task(task)['state'],'running')


class BudgetTests(ControlsFixture):
    def meter(self, **settings):
        self.engine.executor = MeteredExecutor()
        self.executor = self.engine.executor
        self.settings(**settings)

    def test_cached_input_is_a_subset_and_cost_uses_exact_decimals(self):
        usage = token_usage([USAGE, USAGE])
        self.assertEqual(usage['total_tokens'], 24)
        self.assertEqual(usage['cached_input_tokens'], 8)
        self.assertEqual(estimate(usage, PRICES), '0.000044000')
        self.assertIsNone(estimate(usage, None))

    def test_malformed_usage_and_partial_or_failed_stream_remain_unknown(self):
        for bad in ([], [{}], [dict(USAGE, input_tokens=True)], [dict(USAGE, cached_input_tokens=11)],
                    [dict(USAGE, output_tokens=-1)], [dict(USAGE, output_tokens=1.0)],
                    [dict(USAGE, input_tokens=10**80)]):
            self.assertIsNone(token_usage(bad))
        path = self.root/'events.log'
        for tail in ('{"type":"turn.failed"}\n', '{"type":"turn.started"}\n', '{"type":'):
            path.write_text(json.dumps(dict(type='turn.completed',usage=USAGE))+'\n'+tail)
            self.assertIsNone(log_usage(path))

    def test_token_cap_blocks_before_checkout_and_survives_restart(self):
        self.meter(budget=dict(daily_tokens=12))
        self.engine.execute(self.task())
        second = self.engine.enqueue('example/demo', 'Second', 'Return 5.')
        with self.assertRaises(DispatchBlocked) as blocked:
            self.engine.execute(second)
        self.assertEqual(blocked.exception.reason['kind'], 'token_budget')
        self.assertEqual(self.executor.calls, 1)
        other = Engine(Config(self.path))
        try:
            self.assertEqual(other.dispatch_blocker()['kind'], 'token_budget')
            self.assertEqual(Budget(other.config, other.store).summary()['tokens'], 12)
        finally:
            other.store.close()

    def test_estimated_cost_cap_uses_price_snapshot_and_counts_failed_tests(self):
        self.meter(model='fixture-model',budget=dict(daily_estimated_usd='0.00002',prices_per_million=PRICES))
        self.executor.mode = 'wrong'
        with self.assertRaises(ApeeError):
            self.engine.execute(self.task())
        budget = Budget(self.config, self.engine.store)
        self.assertEqual(budget.summary()['estimated_usd'], '0.000022000')
        self.assertEqual(budget.blocker()['kind'], 'cost_budget')
        self.settings(budget=dict(daily_estimated_usd='0.00002',prices_per_million=dict(input='0',cached_input='0',output='0')))
        self.assertEqual(Budget(self.config, self.engine.store).summary()['estimated_usd'], '0.000022000')

    def test_missing_usage_blocks_new_models_but_not_validated_publish(self):
        self.settings(budget=dict(daily_tokens=100))
        task = self.task()
        self.engine.execute(task)
        self.assertEqual(Budget(self.config, self.engine.store).blocker()['kind'], 'usage_unknown')
        self.engine.publish(task)
        self.assertEqual(self.github.creates, 1)

    def test_imported_patch_is_not_model_usage_and_still_obeys_daily_limit(self):
        self.settings(budget=dict(daily_tokens=1),daily_attempt_limit=1)
        (self.seed/'calculator.py').write_text('def add(a, b):\n    return a + b\n')
        patch_file = self.root/'patch.diff'
        self.runner.git(self.seed,'diff','--binary',log=patch_file)
        self.runner.git(self.seed,'restore','calculator.py')
        task = self.task()
        run = self.engine.execute(task,patch=patch_file)
        self.assertEqual(self.engine.store.usage_records(), [])
        self.assertEqual(Budget(self.config,self.engine.store).summary()['tokens'],0)
        with self.assertRaises(ApeeError):
            Budget(self.config,self.engine.store).record_manual(run['id'],USAGE,'0','wrong source')
        second = self.engine.enqueue('example/demo','Second','Return 5.')
        with self.assertRaises(DispatchBlocked):
            self.engine.execute(second,patch=patch_file)

    def test_pending_usage_reconciliation_waits_for_idle_lease_and_is_idempotent(self):
        self.meter()
        run = self.engine.execute(self.task())
        budget = Budget(self.config,self.engine.store)
        budget.begin(run)  # Reproduce an interrupted accounting finalization.
        with attempt_lease(run['run_dir'],create=False):
            budget.reconcile()
            self.assertEqual(self.engine.store.usage_records()[0]['status'],'pending')
        budget.reconcile()
        budget.reconcile()
        self.assertEqual(len(self.engine.store.usage_records()),1)
        self.assertEqual(budget.summary()['tokens'],12)

    def test_legacy_usage_import_keeps_unknown_model_and_cost(self):
        self.meter()
        run = self.engine.execute(self.task())
        self.engine.store.db.execute('delete from usage_records')
        self.engine.store.db.commit()
        budget = Budget(self.config,self.engine.store)
        self.assertEqual(budget.summary()['tokens'],12)
        self.assertIsNone(budget.summary()['estimated_usd'])
        self.assertIsNone(budget.records()[0]['model'])
        budget.reconcile()
        self.assertEqual(len(self.engine.store.usage_records()),1)

    def test_legacy_failed_model_without_agent_metadata_is_not_treated_as_free(self):
        run=self.engine.execute(self.task())
        run.pop('agent')
        self.engine.store.transition(run['task_id'],'failed',run=run)
        self.engine.store.db.execute('delete from usage_records')
        self.engine.store.db.commit()
        (Path(run['run_dir'])/'agent-events.log').write_text('{"type":"turn.failed"}\n')
        self.settings(budget=dict(daily_tokens=100))
        budget=Budget(self.config,self.engine.store)
        self.assertEqual(budget.blocker()['kind'],'usage_unknown')
        self.assertEqual(budget.summary()['unknown_runs'],[run['id']])

    def test_manual_confirmation_is_audited_and_cannot_rewrite_known_tokens(self):
        self.meter()
        run = self.engine.execute(self.task())
        budget = Budget(self.config,self.engine.store)
        with self.assertRaises(ApeeError):
            budget.record_manual(run['id'],USAGE,'0','')
        with self.assertRaises(ApeeError):
            budget.record_manual(run['id'],dict(USAGE,output_tokens=0),'0','reduce known usage')
        saved = budget.record_manual(run['id'],USAGE,'0.5','Checked the selected price schedule')
        self.assertEqual(saved['manual']['previous']['source'],'codex_jsonl')
        self.assertEqual(budget.summary()['estimated_usd'],'0.5')

    def test_unpriced_cache_write_counter_is_preserved_during_manual_cost_audit(self):
        self.meter()
        run=self.engine.execute(self.task())
        event=dict(type='turn.completed',usage=dict(USAGE,cache_write_input_tokens=2))
        (Path(run['run_dir'])/'agent-events.log').write_text(json.dumps(event)+'\n')
        budget=Budget(self.config,self.engine.store)
        budget.finish(run)
        self.assertIsNone(estimate(budget.records()[0]['usage'],PRICES))
        saved=budget.record_manual(run['id'],USAGE,'0.5','Explicit price audit for cache write usage')
        self.assertEqual(saved['usage']['cache_write_input_tokens'],2)

    def test_utc_start_day_controls_bucket_without_clearing_history(self):
        self.meter(budget=dict(daily_tokens=12))
        self.engine.execute(self.task())
        self.assertIsNotNone(Budget(self.config,self.engine.store).blocker())
        with patch('apee.budget.now',return_value='2099-01-01T00:00:00+00:00'):
            summary=Budget(self.config,self.engine.store).summary()
            self.assertEqual(summary['tokens'],0)
            self.assertIsNone(Budget(self.config,self.engine.store).blocker())
        self.assertEqual(len(self.engine.store.usage_records()),1)

    def test_history_keeps_previous_day_usage_and_manual_audit_out_of_today(self):
        self.meter(budget=dict(daily_tokens=12))
        run = self.engine.execute(self.task())
        record = self.engine.store.usage_records()[0]
        record['at'] = '2000-01-01T23:59:59+00:00'
        self.engine.store.save_usage(record)
        budget = Budget(self.config, self.engine.store)
        self.assertEqual(budget.summary()['tokens'], 0)
        self.assertIsNone(budget.blocker())
        self.assertEqual(budget.history()[0]['date'], '2000-01-01')
        self.assertEqual(budget.history()[0]['tokens'], 12)
        self.assertIsNone(budget.history()[0]['estimated_usd'])
        budget.record_manual(run['id'], USAGE, '0.5', 'Historical audit based on fixture rates')
        self.assertEqual(budget.summary('2000-01-01')['estimated_usd'], '0.5')
        self.assertEqual(budget.summary()['tokens'], 0)
        self.assertIsNone(budget.blocker())

    def test_history_is_bounded_but_older_dates_remain_queryable(self):
        from datetime import date, timedelta
        for index in range(42):
            day = (date(2000, 1, 1) + timedelta(days=index)).isoformat()
            self.engine.store.save_usage(dict(run_id=f'fixture-{index}', at=day+'T00:00:00+00:00',
                usage=token_usage([USAGE]), estimated_usd=None, status='known', source='fixture'))
        budget = Budget(self.config, self.engine.store)
        history = budget.history()
        self.assertEqual(len(history), 30)
        self.assertGreater(history[0]['date'], history[-1]['date'])
        self.assertEqual(budget.summary('2000-01-01')['tokens'], 12)
        self.assertEqual(len(budget.records()), 42)

    def test_historical_date_validation_and_cli_do_not_change_ledger(self):
        budget = Budget(self.config, self.engine.store)
        for day in ('2026-02-30', '20261014', '2026-1-1', '', 123):
            with self.subTest(day=day), self.assertRaises(ApeeError):
                budget.summary(day)
        with redirect_stdout(io.StringIO()) as output:
            result = main(['--config', str(self.path), 'budget', 'status', '--date', '2000-01-01'])
        self.assertEqual(result, 0)
        summary, _ = json.JSONDecoder().raw_decode(output.getvalue())
        self.assertEqual(summary['date'], '2000-01-01')
        self.assertEqual(self.engine.store.usage_records(), [])


class SettingsAndDesktopTests(ControlsFixture):
    def test_add_project_preserves_latest_policies_and_raw_templates(self):
        self.data['projects'][0]['tests'] = [[sys.executable, '{config_dir}/check.py']]
        self.settings()
        stale = self.config
        configure(stale, dict(budget=dict(daily_tokens=123)))
        add_project(stale, dict(repo='Example/Second', tests=[['python3', '{config_dir}/new.py']],
                               setup=[], priority=2, publish_draft=False, setup_network=False))
        latest = Config(self.path)
        self.assertEqual(latest.budget, dict(daily_tokens=123))
        self.assertEqual(latest.data['projects'][0], self.data['projects'][0])
        self.assertEqual(latest.data['projects'][1]['tests'][0][1], '{config_dir}/new.py')
        self.assertEqual(latest.project('example/second')['tests'][0][1], str(self.root/'new.py'))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_invalid_or_duplicate_project_leaves_config_unchanged(self):
        original = self.path.read_bytes()
        cases = [dict(repo='example/second', tests=[]),
                 dict(repo='example/second', tests=[['python3']], setup_network='yes'),
                 dict(repo='example/second', tests=[['python3']], priority=6),
                 dict(repo='EXAMPLE/DEMO', tests=[['different-test']])]
        for project in cases:
            with self.subTest(project=project), self.assertRaises(ApeeError):
                add_project(self.config, project)
            self.assertEqual(self.path.read_bytes(), original)

    def test_add_project_rejects_changed_state_directory_and_ignores_old_temp_symlink(self):
        target = self.root/'untouched.txt'
        target.write_text('preserve')
        self.path.with_name(self.path.name+'.tmp').symlink_to(target)
        add_project(self.config, dict(repo='example/second', tests=[['python3']]))
        self.assertEqual(target.read_text(), 'preserve')
        latest = Config(self.path)
        changed = dict(latest.data, state_dir='other-state')
        self.path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ApeeError, '狀態目錄'):
            add_project(latest, dict(repo='example/third', tests=[['python3']]))
        self.assertEqual(json.loads(self.path.read_text()), changed)

    def test_snapshot_exposes_project_policy_and_unexpanded_commands(self):
        self.data['projects'][0]['tests'] = [[sys.executable, '{config_dir}/check.py']]
        self.data['projects'][0]['allowed_paths'] = ['calculator.py']
        self.settings()
        with patch('apee.controls.Service') as service:
            service.return_value.status.return_value = dict(loaded=False)
            result = snapshot(self.engine)
        self.assertEqual(result['projects'][0]['tests'], self.data['projects'][0]['tests'])
        self.assertEqual(result['projects'][0]['allowed_paths'], ['calculator.py'])
        self.assertFalse(result['projects'][0]['publish_draft'])
        self.assertEqual(result['budget_history'], [])

    def test_enqueue_while_engine_is_locked_is_durable_and_deduplicated(self):
        args = ['--config', str(self.path), 'enqueue', 'example/demo',
                '--title=-Title with spaces', '--body=Keep $(literal) and completion criteria.']
        with self.engine.store.lock(), redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
            self.assertEqual(main(args), 0)
            self.assertEqual(main(['--config', str(self.path), 'project-add', 'example/second',
                                   '--test', '["python3"]']), 1)
        tasks = self.engine.store.tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['state'], 'queued')
        self.assertEqual(tasks[0]['spec']['title'], '-Title with spaces')
        self.assertIn('$(literal)', tasks[0]['spec']['body'])
        self.assertEqual(self.engine.store.total_attempts(), 0)
        self.assertEqual(len(self.engine.store.db.execute('SELECT * FROM events').fetchall()), 1)
        self.assertNotIn('example/second', Config(self.path).projects)

    def test_invalid_policies_and_unpriced_cost_limits_are_rejected(self):
        cases=[dict(auto_recovery=dict(enabled='yes')),dict(auto_recovery=dict(max_retries=True)),
               dict(auto_recovery=dict(cooldown_seconds=59)),dict(auto_recovery=dict(max_retries=6)),
               dict(auto_recovery=dict(unknown=True)),dict(budget=dict(daily_tokens=True)),
               dict(budget=dict(daily_tokens=-1)),dict(budget=dict(daily_estimated_usd='10')),
               dict(model='fixture',budget=dict(daily_estimated_usd='NaN',prices_per_million=PRICES)),
               dict(model='fixture',budget=dict(prices_per_million=dict(input='-1',cached_input='1',output='1')))]
        original=self.path.read_bytes()
        for settings in cases:
            with self.subTest(settings=settings),self.assertRaises(ApeeError):
                configure(self.config,settings)
            self.assertEqual(self.path.read_bytes(),original)

    def test_settings_preserve_templates_and_unrelated_project_policy(self):
        self.data['projects'][0]['tests']=[[sys.executable,'{config_dir}/check.py']]
        self.settings()
        configure(self.config,dict(auto_recovery=dict(enabled=True),budget=dict(daily_tokens=123)))
        updated=Config(self.path)
        self.assertEqual(updated.data['projects'],self.data['projects'])
        self.assertEqual(updated.budget['daily_tokens'],123)
        self.assertTrue(updated.auto_recovery['enabled'])
        with self.assertRaises(ApeeError):
            configure(updated,dict(state_dir='other'))

    def test_snapshot_cli_is_one_json_object_and_does_not_poll_github(self):
        before=self.engine.store.total_attempts()
        with patch('apee.controls.Service') as service:
            service.return_value.status.return_value=dict(loaded=False,running=False)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['--config',str(self.path),'snapshot']),0)
        result=json.loads(output.getvalue())
        self.assertEqual(result['version'],__version__)
        self.assertEqual(result['codex'],self.config.codex)
        self.assertEqual(result['tasks'],[])
        self.assertEqual(result['budget']['tokens'],0)
        self.assertEqual(self.engine.store.total_attempts(),before)
        self.assertFalse((self.config.state_dir/'overview.md').exists())

    def test_native_app_builder_uses_private_settings_and_refuses_overwrite(self):
        class Compiler:
            def run(self,args,**kwargs):
                if 'swiftc' in args:
                    Path(args[-1]).write_bytes(b'fixture binary')
                return ''
        output=self.root/'App with spaces.app'
        with patch('apee.desktop.sys.platform','darwin'),patch.dict(os.environ,GH_TOKEN='fixture-secret'):
            result=build(self.config,output,Compiler())
            self.assertEqual(result['app'],str(output))
            backend=json.loads((output/'Contents/Resources/backend.json').read_text())
            self.assertEqual(backend['config'],str(self.path))
            self.assertNotIn('fixture-secret',json.dumps(backend))
            self.assertEqual((output/'Contents/Resources/backend.json').stat().st_mode & 0o777,0o600)
            info=plistlib.loads((output/'Contents/Info.plist').read_bytes())
            self.assertEqual(info['CFBundleIdentifier'],'com.apee.desktop')
            with self.assertRaises(ApeeError):
                build(self.config,output,Compiler())


@unittest.skipUnless(os.environ.get('APEE_NATIVE_TESTS') == '1' and sys.platform == 'darwin', '原生 macOS 驗證')
class NativeAutomaticRecoveryTests(CrashHarness, ControlsFixture):
    def test_native_sandbox_orphan_is_recovered_only_after_lease_release(self):
        self.policy()
        task=self.task()
        process=self.start_controller(task,native=True)
        child,run_dir=None,None
        try:
            child=wait_until(lambda:self.child_ready(task),'native child did not start')
            run_dir=Path(self.engine.store.run(task)['run_dir'])
            process.kill()
            process.wait(timeout=5)
            self.assertEqual(self.engine.auto_recover(),[])
            self.stop_child(child,run_dir)
            self.assertTrue(self.recover_once()[0]['retried'])
            self.assertEqual(self.engine.store.task(task)['state'],'queued')
            self.assertTrue((run_dir/'repo/partial-work.txt').exists())
        finally:
            self.cleanup_controller(process,child,run_dir)


@unittest.skipUnless(os.environ.get('APEE_NATIVE_TESTS') == '1' and sys.platform == 'darwin', '原生 macOS 桌面驗證')
class NativeDesktopTests(unittest.TestCase):
    def test_real_signed_app_executes_private_json_backend(self):
        cache=Path.home()/'.cache'
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='apee-native-desktop-',dir=cache) as temporary:
            root=Path(temporary)
            runtime=root/'runtime with spaces'
            shutil.copytree(Path(__file__).resolve().parents[1]/'apee',runtime/'apee',ignore=shutil.ignore_patterns('__pycache__'))
            config=root/'settings with spaces.json'
            config.write_text(json.dumps(dict(version=1,state_dir='state',projects=[])))
            output=root/'Desktop with spaces.app'
            built=subprocess.run([sys.executable,'-B','-m','apee','--config',str(config),
                                  'desktop','build','--output',str(output)],cwd=runtime,
                                 capture_output=True,text=True,timeout=240)
            self.assertEqual(built.returncode,0,built.stdout+built.stderr)
            result=subprocess.run([str(output/'Contents/MacOS/APEE'),'--diagnose'],
                                  capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            snapshot=json.loads(result.stdout)
            self.assertEqual(snapshot['version'],__version__)
            self.assertEqual(snapshot['tasks'],[])
            self.assertEqual(snapshot['budget']['tokens'],0)
            self.assertFalse(snapshot['service']['loaded'])
