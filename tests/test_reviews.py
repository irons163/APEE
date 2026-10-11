import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from apee.config import ApeeError, Config
from apee.engine import Engine
from apee.github import GitHub
from apee.report import render
from apee.reviews import check_result, snapshot
from apee.store import Store
from apee.worker import work
from test_engine import EngineFixture


def remote(run, **overrides):
    return dict(url=run["pr_url"], state="OPEN", isDraft=True, headRefOid=run["commit"],
                headRefName=run["branch"], baseRefName=run["base"], reviewDecision="",
                mergeStateStatus="CLEAN", statusCheckRollup=[], mergedAt=None, closedAt=None, **overrides)


def check(conclusion="SUCCESS", status="COMPLETED", name="unit tests"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
            "detailsUrl": "https://github.com/example/demo/actions/runs/1"}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="apee-review-")
        self.root = Path(self.tmp.name)
        self.path = self.root / "config.json"
        self.path.write_text(json.dumps(dict(version=1, state_dir="state", projects=[dict(
            repo="example/demo", enabled=True, tests=[["true"]],
        )])))
        self.github = Mock()
        self.engine = Engine(Config(self.path), github=self.github, executor=Mock(), validator=Mock())
        self.remotes = {}
        self.github.pr_status.side_effect = lambda repo, url: copy.deepcopy(self.remotes[url])

    def tearDown(self):
        self.engine.store.close()
        self.tmp.cleanup()

    def delivered(self, number=1):
        task = self.engine.enqueue("example/demo", f"Fixture {number}", "Development completed in prior stage.")
        run = dict(id=f"run-{number}", task_id=task, repo="example/demo", base="main",
                   branch=f"apee/task-{number}", commit=f"{number:040x}", base_sha="0" * 40,
                   pr_url=f"https://github.com/example/demo/pull/{number}",
                   tests=[{"command": ["true"], "passed": True}],
                   run_dir=str(self.root / f"run-{number}"), verified_at="2026-10-11T00:00:00+00:00")
        self.engine.store.transition(task, "review", run=run)
        self.remotes[run["pr_url"]] = remote(run)
        return task, run

    def test_missing_checks_are_never_reported_as_passed(self):
        task, run = self.delivered()
        self.engine.refresh_reviews()
        self.assertEqual(self.engine.store.run(task)["pr_status"]["ci"], "none")
        self.assertIn("未回報檢查", render(self.engine).read_text())
        self.remotes[run["pr_url"]]["statusCheckRollup"] = None
        self.engine.refresh_reviews()
        self.assertEqual(self.engine.store.run(task)["pr_status"]["ci"], "unknown")

    def test_current_check_enums_and_unknown_results_are_conservative(self):
        expected = {"SUCCESS": "passed", "SKIPPED": "skipped", "NEUTRAL": "skipped",
                    "CANCELLED": "cancelled", "FAILURE": "failed", "TIMED_OUT": "failed",
                    "ACTION_REQUIRED": "failed", "STARTUP_FAILURE": "failed", "STALE": "failed",
                    "NEW_FUTURE_VALUE": "unknown", "": "unknown"}
        for conclusion, bucket in expected.items():
            with self.subTest(conclusion=conclusion):
                self.assertEqual(check_result(check(conclusion))["bucket"], bucket)
        for status in ("QUEUED", "IN_PROGRESS", "PENDING", "REQUESTED", "WAITING"):
            self.assertEqual(check_result(check("", status))["bucket"], "pending")
        for state, bucket in (("SUCCESS", "passed"), ("ERROR", "failed"), ("FAILURE", "failed"),
                              ("PENDING", "pending"), ("EXPECTED", "pending"), ("FUTURE", "unknown")):
            self.assertEqual(check_result({"__typename": "StatusContext", "state": state})["bucket"], bucket)
        self.assertEqual(check_result(None)["bucket"], "unknown")
        self.assertEqual(check_result({"__typename": "FutureCheck", "state": "SUCCESS"})["bucket"], "unknown")

    def test_mixed_checks_surface_failures_pending_unknown_and_skips(self):
        _, run = self.delivered()
        cases = [
            ([check(), check("FAILURE")], "failed"),
            ([check(), check("", "IN_PROGRESS")], "pending"),
            ([check(), check("NEW_VALUE")], "unknown"),
            ([check(), check("CANCELLED")], "cancelled"),
            ([check("NEUTRAL"), check("SKIPPED")], "skipped"),
            ([check(), check("SKIPPED")], "passed"),
        ]
        for checks, result in cases:
            with self.subTest(result=result):
                data = remote(run)
                data["statusCheckRollup"] = checks
                self.assertEqual(snapshot(data, run)["ci"], result)

    def test_unchanged_observations_update_freshness_without_duplicate_events(self):
        task, run = self.delivered()
        self.remotes[run["pr_url"]]["statusCheckRollup"] = [check(), check("SKIPPED", name="optional")]
        first = self.engine.refresh_reviews()
        events = self.engine.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.remotes[run["pr_url"]]["statusCheckRollup"].reverse()
        with patch("apee.engine.now", return_value="2026-11-01T00:00:00+00:00"):
            second = self.engine.refresh_reviews()
        self.assertEqual(len(first["changes"]), 1)
        self.assertEqual(second["changes"], [])
        self.assertEqual(self.engine.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], events)
        self.assertEqual(self.engine.store.run(task)["pr_observed_at"], "2026-11-01T00:00:00+00:00")

    def test_remote_head_and_target_changes_never_relabel_original_tests(self):
        task, run = self.delivered()
        self.remotes[run["pr_url"]].update(headRefOid="f" * 40, baseRefName="release", statusCheckRollup=[check()])
        self.engine.refresh_reviews()
        saved = self.engine.store.run(task)
        self.assertFalse(saved["pr_status"]["matches_verified_commit"])
        self.assertFalse(saved["pr_status"]["matches_delivery_target"])
        self.assertEqual(saved["commit"], run["commit"])
        self.assertEqual(saved["tests"], run["tests"])
        self.assertIn("原本機測試不涵蓋目前版本", render(self.engine).read_text())
        self.engine.executor.execute.assert_not_called()
        self.engine.validator.check.assert_not_called()

    def test_review_requests_and_merge_conflicts_are_actionable(self):
        task, run = self.delivered()
        self.remotes[run["pr_url"]].update(reviewDecision="CHANGES_REQUESTED", mergeStateStatus="DIRTY")
        self.engine.refresh_reviews()
        reasons = self.engine.store.run(task)["pr_status"]["attention"]
        self.assertIn("審查者要求修改", reasons)
        self.assertIn("PR 有合併衝突", reasons)
        self.assertIn("需要你處理", render(self.engine).read_text())

    def test_closed_and_merged_are_distinct_and_reopen_requires_explicit_refresh(self):
        task, run = self.delivered()
        self.remotes[run["pr_url"]].update(state="CLOSED", isDraft=False, closedAt="2026-10-11T01:00:00Z")
        self.engine.refresh_reviews()
        self.assertEqual(self.engine.store.task(task)["state"], "closed")
        self.remotes[run["pr_url"]].update(state="OPEN", closedAt=None)
        self.assertEqual(self.engine.refresh_reviews()["checked"], 0)
        self.engine.refresh_reviews(task)
        self.assertEqual(self.engine.store.task(task)["state"], "review")
        self.remotes[run["pr_url"]].update(state="MERGED", mergedAt="2026-10-11T02:00:00Z")
        self.engine.refresh_reviews()
        self.assertEqual(self.engine.store.task(task)["state"], "merged")
        self.assertIsNone(self.engine.next_task())
        self.assertEqual(self.engine.publish(task)["pr_url"], run["pr_url"])
        self.github.push.assert_not_called()
        self.github.create_pr.assert_not_called()
        other = Store(self.engine.config.state_dir)
        try:
            self.assertEqual(other.task(task)["state"], "merged")
        finally:
            other.close()

    def test_read_failures_preserve_last_success_and_recovery_is_recorded_once(self):
        task, run = self.delivered()
        self.remotes[run["pr_url"]]["statusCheckRollup"] = [check()]
        self.engine.refresh_reviews()
        old = self.engine.store.run(task)
        self.github.pr_status.side_effect = ApeeError("GitHub connection unavailable")
        first = self.engine.refresh_reviews()
        saved = self.engine.store.run(task)
        self.assertEqual(saved["pr_status"], old["pr_status"])
        self.assertEqual(saved["pr_observed_at"], old["pr_observed_at"])
        self.assertEqual(self.engine.store.task(task)["state"], "review")
        self.assertEqual(len(first["errors"]), 1)
        events = self.engine.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.engine.refresh_reviews()
        self.assertEqual(self.engine.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], events)
        self.assertIn("更新失敗，以下為上次資訊", render(self.engine).read_text())
        self.github.pr_status.side_effect = lambda repo, url: copy.deepcopy(self.remotes[url])
        self.assertEqual(len(self.engine.refresh_reviews()["changes"]), 1)
        self.assertNotIn("pr_refresh_error", self.engine.store.run(task))
        self.assertEqual(self.engine.refresh_reviews()["changes"], [])

    def test_malformed_or_wrong_pr_data_cannot_overwrite_known_state(self):
        task, run = self.delivered()
        self.engine.refresh_reviews()
        original = copy.deepcopy(self.remotes[run["pr_url"]])
        cases = [{"url": "https://github.com/other/repo/pull/1"}, {"url": "https://github.com/example/demo/pull/999"},
                 {"state": "FUTURE"}, {"headRefOid": None}, {"statusCheckRollup": {}}, {"reviewDecision": []}]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.remotes[run["pr_url"]] = dict(original, **overrides)
                self.assertEqual(len(self.engine.refresh_reviews()["errors"]), 1)
                self.assertEqual(self.engine.store.run(task)["pr_status"]["state"], "OPEN")

    def test_paused_or_quota_blocked_cycle_still_tracks_delivery_without_development(self):
        task, run = self.delivered()
        for key, value in (("codex_hold", {"kind": "usage_limit", "message": "fixture", "log": "fixture"}),
                           ("paused", {"reason": "fixture"})):
            self.engine.store.set_control(key, value)
            result = self.engine.cycle(local_only=True)
            self.assertEqual(result["review_checked"], 1)
            self.assertEqual(result["attempts"], 0)
            self.assertIn("blocked", result)
        self.github.issues.assert_not_called()
        self.engine.executor.execute.assert_not_called()
        self.assertEqual(self.engine.store.total_attempts(), 0)

    def test_periodic_tracking_is_bounded_and_rotates_through_oldest_observations(self):
        for number in range(1, 4):
            self.delivered(number)
        self.engine.config.max_pr_checks = 2
        self.engine.store.set_control("paused", {"reason": "fixture"})
        self.assertEqual(self.engine.cycle()["review_checked"], 2)
        self.assertEqual(self.engine.cycle()["review_checked"], 2)
        urls = {call.args[1] for call in self.github.pr_status.call_args_list}
        self.assertEqual(len(urls), 3)

    def test_disabled_projects_are_not_polled(self):
        task, _ = self.delivered()
        self.engine.config.projects["example/demo"]["enabled"] = False
        self.assertEqual(self.engine.refresh_reviews()["checked"], 0)
        self.github.pr_status.assert_not_called()
        with self.assertRaises(ApeeError):
            self.engine.refresh_reviews(task)

    def test_one_pr_read_failure_does_not_hide_another_pr_update(self):
        first, run1 = self.delivered(1)
        second, run2 = self.delivered(2)
        self.remotes[run2["pr_url"]].update(state="MERGED", mergedAt="2026-10-11T02:00:00Z")

        def fetch(repo, url):
            if url == run1["pr_url"]:
                raise ApeeError("fixture unavailable")
            return copy.deepcopy(self.remotes[url])

        self.github.pr_status.side_effect = fetch
        result = self.engine.refresh_reviews()
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(self.engine.store.task(first)["state"], "review")
        self.assertEqual(self.engine.store.task(second)["state"], "merged")

    def test_worker_reports_pr_changes_once_and_repeated_read_errors_once(self):
        _, run = self.delivered()
        self.engine.store.set_control("paused", {"reason": "fixture"})
        messages = []
        work(self.engine, max_cycles=3, sleep=lambda _: None, notify=messages.append)
        self.assertEqual(sum(bool(m.get("pr_updates")) for m in messages), 1)
        self.github.pr_status.side_effect = ApeeError("fixture connection failure")
        messages = []
        work(self.engine, max_cycles=3, sleep=lambda _: None, notify=messages.append)
        self.assertEqual(sum(bool(m.get("errors")) for m in messages), 1)

    def test_github_reader_uses_saved_pr_number_not_a_moving_branch(self):
        runner = Mock()
        runner.run.return_value = "{}"
        GitHub(runner).pr_status("example/demo", "https://github.com/example/demo/pull/42")
        args = runner.run.call_args.args[0]
        self.assertEqual(args[:6], ["gh", "pr", "view", "42", "--repo", "example/demo"])
        for url in ("https://github.com/elsewhere/demo/pull/42", "https://evil.test/example/demo/pull/42",
                    "https://github.com/example/demo/pull/42?other=1"):
            with self.assertRaises(ApeeError):
                GitHub(runner).pr_status("example/demo", url)
        self.assertEqual(runner.run.call_count, 1)


class DeliveryTrackingIntegration(EngineFixture):
    def test_verified_delivery_can_be_tracked_without_another_model_run_or_push(self):
        self.config.projects["example/demo"]["publish_draft"] = True
        task = self.task()
        run = self.engine.execute(task)
        self.github.pr_status = Mock(return_value=remote(run))
        self.engine.refresh_reviews()
        self.assertTrue(self.engine.store.run(task)["pr_status"]["matches_verified_commit"])
        self.assertEqual(self.executor.calls, 1)
        self.assertEqual(self.github.pushes, 1)
        self.assertEqual(self.github.creates, 1)
        self.assertEqual(self.engine.store.total_attempts(), 1)
