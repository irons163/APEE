import json
import unittest
from pathlib import Path
from unittest.mock import patch

from apee.config import ApeeError
from apee.notifications import Notifications, deliver
from apee.engine import Engine
from apee.store import Store
from test_controls import ControlsFixture
from test_reviews import remote, check


class NotificationTests(ControlsFixture):
    def setUp(self):
        super().setUp()
        self.settings(notifications=dict(enabled=True, max_attempts=2))
        self.notifier = Notifications(self.engine)
        self.notifier.collect()

    def ready(self):
        task = self.task()
        self.engine.execute(task)
        self.notifier.collect()
        return task, next(x for x in self.notifier.pending() if x['topic'].startswith('task:'))

    def test_new_results_persist_and_unchanged_poll_never_duplicates(self):
        task, item = self.ready()
        count = len(self.notifier.records())
        self.notifier.collect()
        self.assertEqual(len(self.notifier.records()), count)
        other = Store(self.config.state_dir)
        try:
            row = other.db.execute('SELECT data FROM notification_outbox WHERE id=?', (item['id'],)).fetchone()
            self.assertEqual(json.loads(row[0])['status'], 'pending')
        finally: other.close()

    def test_claim_receipt_and_restart_are_durable_and_old_token_is_rejected(self):
        _, item = self.ready()
        claimed = self.notifier.claim(item['id'])
        self.assertTrue(claimed['claimed'])
        self.assertFalse(self.notifier.claim(item['id'])['claimed'])
        self.assertFalse(self.notifier.receipt(item['id'], 'wrong')['accepted'])
        self.assertTrue(self.notifier.receipt(item['id'], claimed['token'])['accepted'])
        self.notifier.collect()
        self.assertNotIn(item['id'], [x['id'] for x in self.notifier.pending()])
        self.assertFalse(self.notifier.receipt(item['id'], claimed['token'])['accepted'])

    def test_expired_claim_retries_same_os_identifier_and_failure_backoff_is_bounded(self):
        _, item = self.ready()
        with patch('apee.notifications.now', return_value='2026-10-13T00:00:00+00:00'):
            first = self.notifier.claim(item['id'])
        second = self.notifier.claim(item['id'])
        self.assertTrue(second['claimed'])
        self.assertEqual(second['id'], first['id'])
        self.assertNotEqual(second['token'], first['token'])
        self.assertFalse(self.notifier.receipt(item['id'], first['token'])['accepted'])
        self.notifier.receipt(item['id'], second['token'], 'Synthetic notification denial')
        self.assertEqual(self.notifier.pending(), [])
        self.notifier.retry(item['id'])
        self.assertTrue(self.notifier.claim(item['id'])['claimed'])

    def test_resolved_or_published_results_supersede_obsolete_pending_message(self):
        task, item = self.ready()
        self.engine.publish(task)
        self.notifier.collect()
        self.assertNotIn(item['id'], [x['id'] for x in self.notifier.pending()])
        self.assertEqual(next(x for x in self.notifier.records() if x['id']==item['id'])['status'], 'superseded')
        pr = [x for x in self.notifier.pending() if x['topic'].startswith('pr:')]
        self.assertEqual(len(pr), 1)

    def test_two_tasks_tracking_same_pr_generate_one_semantic_update(self):
        first, _ = self.ready()
        run = self.engine.publish(first)
        other = self.engine.enqueue('example/demo', 'CI child', 'Synthetic linked result')
        child = dict(run, id='linked', task_id=other)
        self.engine.store.transition(other, 'review', dict(url=run['pr_url']), run=child)
        self.notifier.collect()
        self.assertEqual(len([x for x in self.notifier.pending() if x['topic'].startswith('pr:')]), 1)
        self.github.pr_status = lambda repo, url: dict(remote(run), statusCheckRollup=[check('FAILURE')])
        self.engine.refresh_reviews()
        self.notifier.collect()
        self.assertEqual(len([x for x in self.notifier.pending() if x['topic'].startswith('pr:')]), 1)

    def test_disabled_or_unconfigured_notification_never_invokes_external_process(self):
        self.ready()
        with patch.object(self.runner, 'run') as run:
            deliver(self.engine)
            run.assert_not_called()
        self.assertIn('尚未設定', self.notifier.snapshot()['adapter_error']['message'])
        self.settings(notifications=dict(enabled=False))
        with patch.object(self.runner, 'run') as run:
            deliver(self.engine)
            run.assert_not_called()

    def test_first_enable_does_not_replay_old_history_and_permission_failure_is_visible(self):
        self.settings(notifications=dict(enabled=False))
        self.engine.execute(self.task())
        self.settings(notifications=dict(enabled=True))
        self.engine.store.set_control('notification_cursor', None)
        self.notifier = Notifications(self.engine)
        self.notifier.collect()
        self.assertEqual(self.notifier.pending(), [])
        task = self.engine.enqueue('example/demo', 'New result', 'New accepted arithmetic requirement')
        self.engine.execute(task)
        self.notifier.collect()
        item = self.notifier.claim(self.notifier.pending()[0]['id'])
        self.notifier.receipt(item['id'], item['token'], 'Notification authorization denied')
        self.assertEqual(self.notifier.snapshot()['failed'], 1)
        self.assertIn('authorization denied', next(x for x in self.notifier.records() if x['id']==item['id'])['error'])

    def test_expired_last_claim_cannot_overwrite_concurrent_receipt_from_another_connection(self):
        self.settings(notifications=dict(enabled=True, max_attempts=1))
        self.notifier = Notifications(self.engine)
        _, item = self.ready()
        with patch('apee.notifications.now', return_value='2026-10-13T00:00:00+00:00'):
            claim = self.notifier.claim(item['id'])
        other = Engine(self.config)
        records = self.notifier.records
        def interleave():
            old = records()
            self.assertTrue(Notifications(other).receipt(item['id'], claim['token'])['accepted'])
            return old
        try:
            with patch.object(self.notifier, 'records', side_effect=interleave):
                self.assertEqual(self.notifier.pending(), [])
            saved = next(x for x in records() if x['id']==item['id'])
            self.assertEqual(saved['status'], 'delivered')
            self.assertEqual(saved['attempts'], 1)
        finally: other.store.close()

    def test_manual_retry_cannot_erase_a_concurrent_new_claim(self):
        _, item = self.ready()
        with patch('apee.notifications.now', return_value='2026-10-13T00:00:00+00:00'):
            first = self.notifier.claim(item['id'])
            self.notifier.receipt(item['id'], first['token'], 'Synthetic recoverable denial')
        other = Engine(self.config)
        records = self.notifier.records
        second = {}
        def interleave():
            old = records()
            second.update(Notifications(other).claim(item['id']))
            self.assertTrue(second['claimed'])
            return old
        try:
            with patch.object(self.notifier, 'records', side_effect=interleave):
                with self.assertRaisesRegex(ApeeError, '另一程序更新'): self.notifier.retry(item['id'])
            saved = next(x for x in records() if x['id']==item['id'])
            self.assertEqual(saved['status'], 'sending')
            self.assertEqual(saved['token'], second['token'])
            self.assertEqual(saved['attempts'], 2)
        finally: other.store.close()
