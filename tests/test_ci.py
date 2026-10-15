import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from apee import ci
from apee.budget import Budget
from apee.config import ApeeError, DispatchBlocked
from apee.github import GitHub, CINotStarted
from apee.process import Runner
from test_controls import ControlsFixture, MeteredExecutor
from test_quality import Reviewer
from test_reviews import check, remote


class CIRepairTests(ControlsFixture):
    def setUp(self):
        super().setUp()
        self.settings(ci_repair=dict(enabled=True, max_attempts_per_pr=1))
        self.engine.executor = MeteredExecutor()
        self.engine.reviewer = Reviewer()
        self.parent = self.task()
        self.original = self.engine.execute(self.parent)
        self.engine.publish(self.parent)
        self.observed = remote(self.engine.store.run(self.parent))
        self.observed['statusCheckRollup'] = [check('FAILURE')]
        self.github.pr_status = lambda repo, url: copy.deepcopy(self.observed)
        self.github.failure_logs = Mock(return_value=[dict(run_id=1, head=self.original['commit'],
            digest='synthetic-evidence', text='Synthetic CI requires a module docstring.')])
        self.updates = 0
        self.ambiguous = False
        original_clone = self.github.clone

        def clone(repo, workspace, branch):
            if branch == self.original['branch']:
                self.runner.run(['git', 'clone', '--branch', branch, self.original['workspace'], str(workspace)])
            else:
                original_clone(repo, workspace, branch)
        self.github.clone = clone

        def update(repo, workspace, branch, old, commit):
            if self.observed['headRefOid'] != old:
                raise ApeeError('Remote changed')
            self.runner.git(workspace, 'merge-base', '--is-ancestor', old, commit)
            self.updates += 1
            self.observed['headRefOid'] = commit
            if self.ambiguous:
                raise ApeeError('Connection lost after remote accepted update')
        self.github.push_update = update

        class Repair(MeteredExecutor):
            def execute(inner, workspace, *args):
                result = super().execute(workspace, *args)
                with (workspace/'calculator.py').open('a') as out: out.write('# CI evidence resolved\n')
                return result
        self.engine.executor = Repair()

    def scheduled(self):
        self.engine.refresh_reviews()
        result = ci.schedule(self.engine)
        self.assertEqual(result['errors'], [])
        self.assertEqual(len(result['added']), 1)
        return result['added'][0]

    def test_owned_ci_failure_repairs_same_pr_with_fresh_tests_review_and_history(self):
        child = self.scheduled()
        self.assertEqual(ci.schedule(self.engine)['added'], [])
        run = self.engine.execute(child)
        self.assertTrue(run['quality_required'])
        self.assertEqual(run['base_sha'], self.original['commit'])
        self.assertTrue(run['code_review']['approved'])
        self.engine.publish(child)
        self.assertEqual(self.updates, 1)
        self.assertEqual(self.github.creates, 1)
        saved = self.engine.store.run(self.parent)
        self.assertEqual(saved['commit'], self.original['commit'])
        self.assertEqual(saved['tests'], self.original['tests'])
        self.assertEqual(saved['active_delivery']['commit'], run['commit'])
        self.engine.refresh_reviews()
        status = self.engine.store.run(self.parent)['pr_status']
        self.assertTrue(status['matches_verified_commit'])
        self.assertEqual(status['verification_run'], run['id'])
        self.assertEqual(ci.schedule(self.engine)['added'], [])
        self.assertEqual(len(self.engine.store.usage_records()), 3)
        self.assertEqual(Budget(self.config, self.engine.store).summary()['tokens'], 36)

    def test_changed_or_closed_source_stops_before_model(self):
        child = self.scheduled()
        self.observed['headRefOid'] = 'f'*40
        with self.assertRaisesRegex(ApeeError, '來源、版本或狀態'): self.engine.execute(child)
        self.assertEqual(self.engine.executor.calls, 0)
        self.assertEqual(self.updates, 0)

    def test_changed_remote_after_review_is_never_overwritten(self):
        child = self.scheduled()
        self.engine.execute(child)
        self.observed['headRefOid'] = 'e'*40
        with self.assertRaisesRegex(ApeeError, '其他提交'): self.engine.publish(child)
        self.assertEqual(self.updates, 0)
        self.assertNotIn('active_delivery', self.engine.store.run(self.parent))

    def test_ambiguous_push_reconciles_without_repeated_model_or_remote_write(self):
        child = self.scheduled()
        self.engine.execute(child)
        self.ambiguous = True
        with self.assertRaisesRegex(ApeeError, 'Connection lost'): self.engine.publish(child)
        self.assertEqual(self.engine.store.task(child)['state'], 'publishing')
        self.engine.publish(child)
        self.assertEqual(self.updates, 1)
        self.assertEqual(self.engine.executor.calls, 1)
        self.assertEqual(self.engine.store.task(child)['state'], 'review')

    def test_stale_unverifiable_disabled_or_exhausted_failures_do_not_add_work(self):
        self.engine.refresh_reviews()
        self.github.failure_logs.side_effect = ApeeError('No verified logs')
        self.assertEqual(len(ci.schedule(self.engine)['errors']), 1)
        self.assertIsNone(self.engine.store.control('ci_repair:'+self.parent))
        self.github.failure_logs.side_effect = None
        self.observed['headRefOid'] = 'f'*40
        self.engine.refresh_reviews()
        self.assertEqual(ci.schedule(self.engine)['added'], [])
        self.observed['headRefOid'] = self.original['commit']
        child = self.scheduled()
        self.engine.store.transition(child, 'failed')
        self.assertEqual(ci.schedule(self.engine)['added'], [])

    def test_budget_and_pause_still_apply_to_ci_repair(self):
        child = self.scheduled()
        self.engine.store.set_control('paused', dict(message='User paused'))
        with self.assertRaises(DispatchBlocked): self.engine.execute(child)
        self.assertEqual(self.engine.executor.calls, 0)

    def test_failed_repair_can_retry_same_revision_only_up_to_persistent_pr_limit(self):
        self.settings(ci_repair=dict(enabled=True, max_attempts_per_pr=2))
        first = self.scheduled()
        self.assertEqual(ci.schedule(self.engine)['added'], [])
        self.engine.store.transition(first, 'failed', dict(error='Synthetic persistent review rejection'))
        second = ci.schedule(self.engine)['added'][0]
        self.assertNotEqual(first, second)
        self.assertEqual(self.engine.store.task(second)['spec']['expected_head'], self.original['commit'])
        self.assertEqual(self.engine.store.task(second)['spec']['ci_repair_attempt'], 2)
        self.engine.store.transition(second, 'failed')
        self.assertEqual(ci.schedule(self.engine)['added'], [])
        self.assertEqual(self.engine.store.control('ci_repair_status:'+self.parent)['kind'], 'limit')

    def test_waiting_or_interrupted_repair_deduplicates_and_manual_retry_cannot_compete(self):
        self.settings(ci_repair=dict(enabled=True, max_attempts_per_pr=2))
        first = self.scheduled()
        for state in ('waiting', 'interrupted'):
            self.engine.store.transition(first, state)
            self.assertEqual(ci.schedule(self.engine)['added'], [])
            self.assertEqual(self.engine.store.control('ci_repair_status:'+self.parent)['kind'], 'in_progress')
        self.engine.store.transition(first, 'failed')
        second = ci.schedule(self.engine)['added'][0]
        with self.assertRaisesRegex(ApeeError, '未重複派工'): self.engine.retry(first)
        self.assertEqual(self.engine.store.task(first)['state'], 'failed')
        self.engine.store.transition(first, 'queued')  # Simulate an older queue restored during upgrade.
        with self.assertRaisesRegex(ApeeError, '未重複派工'): self.engine.execute(first)
        self.assertEqual(self.engine.executor.calls, 0)
        self.assertEqual(self.engine.store.task(second)['state'], 'queued')


class CILogTests(unittest.TestCase):
    def test_platform_refusal_without_runner_or_steps_is_reported_without_repair(self):
        runner = Mock()
        head = 'a'*40
        pr = dict(state='OPEN', headRefOid=head, statusCheckRollup=[check('FAILURE')])
        data = dict(head_sha=head, status='completed', conclusion='failure', repository=dict(full_name='example/demo'))
        jobs = dict(jobs=[dict(id=42, run_id=1, steps=[], runner_id=0, conclusion='failure', check_run_url='https://api.github.com/repos/example/demo/check-runs/42')])
        runner.run.side_effect = [json.dumps(pr), json.dumps(data), ApeeError('Log unavailable'), json.dumps(jobs),
            json.dumps([dict(annotation_level='failure', message='Account spending limit prevented runner startup.')])]
        with self.assertRaisesRegex(CINotStarted, '未開始程式修復'):
            GitHub(runner).failure_logs('example/demo', 'https://github.com/example/demo/pull/8', head)
        self.assertEqual(runner.run.call_count, 5)

    def test_logs_require_same_repo_revision_and_are_redacted(self):
        runner = Mock()
        head = 'a'*40
        pr = dict(state='OPEN', headRefOid=head, statusCheckRollup=[check('FAILURE')])
        data = dict(head_sha='merge-sha', status='completed', conclusion='failure',
            repository=dict(full_name='example/demo'), html_url='https://github.com/example/demo/actions/runs/1',
            pull_requests=[dict(number=8, head=dict(sha=head))])
        runner.run.side_effect = [json.dumps(pr), json.dumps(data), 'FAILURE password=hidden-value']
        result = GitHub(runner).failure_logs('example/demo', 'https://github.com/example/demo/pull/8', head)
        self.assertEqual(result[0]['head'], head)
        self.assertNotIn('hidden-value', result[0]['text'])
        self.assertEqual(len(result[0]['digest']), 64)
        data['pull_requests'][0]['head']['sha'] = 'b'*40
        runner.run.side_effect = [json.dumps(pr), json.dumps(data)]
        with self.assertRaisesRegex(ApeeError, '來源無法核對'):
            GitHub(runner).failure_logs('example/demo', 'https://github.com/example/demo/pull/8', head)
        self.assertEqual(runner.run.call_count, 5)

    def test_external_log_links_are_not_followed(self):
        runner = Mock()
        bad = check('FAILURE')
        bad['detailsUrl'] = 'https://github.com/another/repo/actions/runs/1'
        runner.run.return_value = json.dumps(dict(state='OPEN', headRefOid='a', statusCheckRollup=[bad]))
        with self.assertRaises(ApeeError): GitHub(runner).failure_logs('example/demo', 'https://github.com/example/demo/pull/8', 'a')
        self.assertEqual(runner.run.call_count, 1)


class GitUpdateTests(unittest.TestCase):
    def test_real_remote_append_and_concurrent_writer_lease(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            actual = Runner()
            bare, work = root/'remote.git', root/'work'
            actual.run(['git', 'init', '--bare', str(bare)])
            actual.run(['git', 'clone', str(bare), str(work)])
            actual.git(work, 'checkout', '-b', 'apee/owned')
            def commit(text):
                (work/'file').write_text(text)
                actual.git(work, 'add', 'file')
                actual.git(work, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-m', text)
                return actual.git(work, 'rev-parse', 'HEAD')
            old = commit('old')
            actual.git(work, 'push', 'origin', 'apee/owned')
            new = commit('new')
            class Redirect:
                race = False
                def git(inner, workspace, *args, **kwargs):
                    args = [str(bare) if x == 'https://github.com/example/demo.git' else x for x in args]
                    if inner.race and 'push' in args:
                        actual.git(bare, 'update-ref', 'refs/heads/apee/owned', human)
                    return actual.git(workspace, *args, **kwargs)
            redirect = Redirect()
            github = GitHub(redirect)
            github.push_update('example/demo', work, 'apee/owned', old, new)
            self.assertEqual(actual.git(bare, 'rev-parse', 'apee/owned'), new)
            actual.git(bare, 'update-ref', 'refs/heads/apee/owned', old)
            human = commit('human')
            actual.git(work, 'push', 'origin', human+':refs/heads/human')
            redirect.race = True
            with self.assertRaises(ApeeError): github.push_update('example/demo', work, 'apee/owned', old, new)
            self.assertEqual(actual.git(bare, 'rev-parse', 'apee/owned'), human)
