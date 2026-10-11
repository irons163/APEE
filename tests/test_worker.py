import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock, patch

from apee.config import ApeeError, BusyError, Config, DispatchBlocked
from apee.executors import CodexExecutor
from apee.report import render
from apee.store import Store
from apee.worker import work
from test_engine import EngineFixture


class WorkerTests(EngineFixture):
    def configure(self, **values):
        self.data.update(values)
        self.path.write_text(json.dumps(self.data))
        self.engine.reload_config()

    def enqueue_many(self, count):
        return [self.engine.enqueue("example/demo", f"Fix addition {i}", "2 + 3 must equal 5.")
                for i in range(count)]

    def cli(self, *args):
        return [sys.executable, "-B", "-m", "apee", "--config", str(self.path), *args]

    def install_quota_fixture(self):
        # A real subprocess failure, with the same structured error shape as the observed CLI.
        program = self.root / "quota-cli"
        event = {"type": "turn.failed", "error": {"message": "You’ve hit your usage limit. Try again at 2:21 PM."}}
        program.write_text(f"#!{sys.executable}\nimport sys\nprint({json.dumps(event)!r})\nsys.exit(1)\n")
        program.chmod(0o700)
        self.configure(codex=str(program), max_tasks_per_cycle=3)
        self.engine.executor = CodexExecutor(self.engine.config, self.runner)

    def test_quota_stops_remaining_queue_and_survives_restart(self):
        self.install_quota_fixture()
        self.enqueue_many(2)
        result = self.engine.cycle(local_only=True)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["blocked"]["kind"], "usage_limit")
        states = [t["state"] for t in self.engine.store.tasks()]
        self.assertEqual(states.count("waiting"), 1)
        self.assertEqual(states.count("queued"), 2)
        other = Store(self.engine.config.state_dir)
        try:
            self.assertEqual(other.control("codex_hold")["kind"], "usage_limit")
            self.assertEqual(other.attempts_today()["used"], 1)
        finally:
            other.close()
        self.assertEqual(self.engine.cycle()["attempts"], 0)
        self.assertEqual(len(list((self.engine.config.state_dir / "runs").iterdir())), 1)
        self.assertEqual(self.github.pushes, 0)

    def test_resume_requires_explicit_codex_flag_and_preserves_attempt(self):
        self.install_quota_fixture()
        self.engine.cycle(local_only=True)
        task = self.engine.store.tasks()[0]
        run_id = task["run_id"]
        result = subprocess.run(self.cli("resume"), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(self.engine.store.control("codex_hold"))
        self.assertEqual(self.engine.store.task(task["id"])["state"], "waiting")
        result = subprocess.run(self.cli("resume", "--codex"), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.engine.store.control("codex_hold"))
        self.assertEqual(self.engine.store.task(task["id"])["state"], "queued")
        self.assertEqual(self.engine.store.task(task["id"])["run_id"], run_id)
        self.assertEqual(self.engine.store.attempts_today()["used"], 1)

    def test_daily_limit_counts_failures_and_resets_only_on_new_utc_day(self):
        self.configure(daily_attempt_limit=1, max_tasks_per_cycle=3)
        self.executor.mode = "wrong"
        self.enqueue_many(2)
        with patch("apee.store.now", return_value="2026-10-11T23:59:59+00:00"):
            first = self.engine.cycle(local_only=True)
            self.assertEqual(first["attempts"], 1)
            self.assertEqual(first["blocked"]["kind"], "daily_limit")
            task = self.engine.next_task()
            with self.assertRaises(DispatchBlocked):
                self.engine.execute(task)
            self.assertEqual(self.executor.calls, 1)
            other = Store(self.engine.config.state_dir)
            try:
                self.assertEqual(other.attempts_today()["used"], 1)
            finally:
                other.close()
        self.executor.mode = "fix"
        with patch("apee.store.now", return_value="2026-10-12T00:00:00+00:00"):
            self.assertIsNone(self.engine.dispatch_blocker())
            second = self.engine.cycle(local_only=True)
            self.assertEqual(second["attempts"], 1)
            self.assertEqual(len(second["runs"]), 1)

    def test_pause_during_execution_prevents_next_task_and_allows_current_delivery(self):
        self.configure(max_tasks_per_cycle=3)
        self.engine.config.projects["example/demo"]["publish_draft"] = True
        self.enqueue_many(2)
        original = self.executor.execute

        def pause_then_finish(*args):
            # Separate CLI process can pause even while the task owns engine.lock.
            paused = subprocess.run(self.cli("pause", "--reason", "Review current work"),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(paused.returncode, 0, paused.stderr)
            return original(*args)

        with patch.object(self.executor, "execute", side_effect=pause_then_finish):
            with self.engine.store.lock():
                result = self.engine.cycle()
        self.assertEqual(result["blocked"]["kind"], "paused")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(self.github.creates, 1)
        self.assertIsNotNone(self.engine.next_task())

    def test_imported_patch_bypasses_provider_hold_but_not_pause_or_daily_limit(self):
        self.configure(daily_attempt_limit=1)
        task = self.task()
        self.engine.store.set_control("codex_hold", {"kind": "usage_limit", "message": "fixture hold", "log": "fixture"})
        (self.seed / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
        patch_file = self.root / "import.patch"
        self.runner.git(self.seed, "diff", "--binary", log=patch_file)
        self.runner.git(self.seed, "restore", "calculator.py")
        self.engine.store.set_control("paused", {"reason": "fixture"})
        with self.assertRaises(DispatchBlocked):
            self.engine.execute(task, patch=patch_file)
        self.engine.store.set_control("paused", None)
        run = self.engine.execute(task, patch=patch_file)
        self.assertEqual(run["agent"]["backend"], "imported_patch")
        second = self.engine.enqueue("example/demo", "Another task", "Fix addition.")
        with self.assertRaises(DispatchBlocked):
            self.engine.execute(second, patch=patch_file)
        self.assertEqual(self.engine.store.task(second)["state"], "queued")

    def test_worker_session_cap_limits_large_cycles(self):
        self.configure(max_tasks_per_cycle=20)
        self.enqueue_many(4)
        sleep = Mock()
        result = work(self.engine, max_cycles=5, max_tasks=2, local_only=True, sleep=sleep)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["cycles"], 1)
        self.assertEqual(result["stop_reason"], "attempt_limit")
        self.assertEqual(self.executor.calls, 2)
        sleep.assert_not_called()
        self.assertIsNotNone(self.engine.next_task())
        self.assertFalse(self.engine.store.worker_active())

    def test_worker_refreshes_config_and_releases_task_lock_between_cycles(self):
        self.enqueue_many(2)

        def disable_project(_seconds):
            other = Store(self.engine.config.state_dir)
            try:
                with other.lock():
                    self.data["projects"][0]["enabled"] = False
                    self.path.write_text(json.dumps(self.data))
            finally:
                other.close()

        result = work(self.engine, max_cycles=2, max_tasks=5, local_only=True, sleep=disable_project)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["cycles"], 2)
        self.assertEqual(self.executor.calls, 1)
        self.assertTrue(any(t["state"] == "queued" for t in self.engine.store.tasks()))

    def test_worker_lock_prevents_second_loop(self):
        other = Store(self.engine.config.state_dir)
        try:
            with other.lock("worker"):
                with self.assertRaises(BusyError):
                    work(self.engine, max_cycles=1)
            self.assertEqual(self.executor.calls, 0)
        finally:
            other.close()

    def test_worker_defers_a_cycle_while_another_task_holds_lock(self):
        self.task()
        with self.engine.store.lock():
            result = work(self.engine, max_cycles=1)
        self.assertTrue(result["last_result"]["busy"])
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(self.executor.calls, 0)

    def test_interruption_during_development_still_counts_started_attempt(self):
        self.executor.mode = "interrupt"
        with self.assertRaises(KeyboardInterrupt):
            work(self.engine, max_cycles=1)
        status = self.engine.store.control("worker")
        self.assertEqual(status["cycles"], 1)
        self.assertEqual(status["attempts"], 1)
        self.assertEqual(status["state"], "interrupted")
        self.assertEqual(self.engine.store.tasks()[0]["state"], "interrupted")
        self.assertFalse(self.engine.store.worker_active())

    def test_worker_idle_polls_are_quiet_and_bounded(self):
        self.github.issues = Mock(return_value=[])
        messages, sleeps = [], []
        result = work(self.engine, interval=3, max_cycles=4, sleep=sleeps.append, notify=messages.append)
        self.assertEqual(result["cycles"], 4)
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(sleeps, [3, 3, 3])
        self.assertEqual(len(messages), 3)  # Started, first idle result, stopped.
        self.assertEqual(result["stop_reason"], "cycle_limit")

    def test_blocked_worker_does_not_poll_github_or_retry_codex(self):
        self.install_quota_fixture()
        self.engine.cycle(local_only=True)
        self.github.issues = Mock(side_effect=AssertionError("unexpected network call"))
        result = work(self.engine, max_cycles=3, sleep=lambda _: None)
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(result["last_result"]["blocked"]["kind"], "usage_limit")
        self.github.issues.assert_not_called()
        self.assertIn("需要處理", render(self.engine).read_text())

    def test_worker_sigterm_stops_wait_and_releases_locks(self):
        self.engine.store.set_control("paused", {"reason": "no external execution in this test"})
        process = subprocess.Popen(self.cli("work", "--interval", "30", "--max-cycles", "100"),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = self.engine.store.control("worker")
                if state and state["cycles"] >= 1:
                    break
                time.sleep(0.03)
            else:
                self.fail("worker did not start")
            self.assertTrue(self.engine.store.worker_active())
            process.send_signal(signal.SIGTERM)
            output, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130, output + error)
            self.assertEqual(self.engine.store.control("worker")["state"], "interrupted")
            self.assertFalse(self.engine.store.worker_active())
            with self.engine.store.lock():
                pass
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def test_worker_refuses_state_directory_change(self):
        self.github.issues = Mock(return_value=[])

        def replace_state(_seconds):
            self.data["state_dir"] = "other-state"
            self.path.write_text(json.dumps(self.data))

        with self.assertRaisesRegex(ApeeError, "state_dir"):
            work(self.engine, max_cycles=2, sleep=replace_state)
        self.assertEqual(self.engine.store.control("worker")["state"], "failed")
        self.assertFalse((self.root / "other-state").exists())

    def test_daily_limit_configuration_rejects_invalid_values(self):
        for value in (0, -1, True, "10", 1.5, 1001):
            with self.subTest(value=value):
                self.data["daily_attempt_limit"] = value
                self.path.write_text(json.dumps(self.data))
                with self.assertRaises(ApeeError):
                    Config(self.path)
