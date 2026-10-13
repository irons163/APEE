import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from apee.config import ApeeError, BusyError, DispatchBlocked
from apee.leases import lease_state
from apee.process import Runner
from apee.report import render
from test_engine import EngineFixture


ROOT = Path(__file__).resolve().parents[1]


def wait_until(check, message, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(message)


class CrashHarness:
    def cli(self, *args):
        return [sys.executable, "-B", "-m", "apee", "--config", str(self.path), *args]

    def event_count(self):
        return self.engine.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def start_controller(self, task_id, phase="running", native=False):
        program = self.root / "crash-controller.py"
        program.write_text(
            f"import sys\nsys.path[:0] = {[str(ROOT), str(ROOT / 'tests')]!r}\n" + '''
import json
from pathlib import Path
from apee.config import Config
from apee.engine import Engine
from apee.executors import Validator
from apee.process import Runner
from test_engine import LocalGitHub, FixExecutor, RealCommandValidator

path, seed, task, phase, native = sys.argv[1:]
config, runner = Config(path), Runner()

def stall(workspace):
    code = (
        "import json,os,time; from pathlib import Path; "
        "Path('partial-work.txt').write_text('preserved'); "
        "Path('child-ready.json').write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp()})); "
        "time.sleep(60)"
    )
    if native == '1':
        Validator(config, runner).check(workspace, workspace.parent, [[sys.executable, '-c', code]], 'stall')
    else:
        runner.run([sys.executable, '-c', code], cwd=workspace,
                   log=workspace.parent / 'stall.log', timeout=50)

class SlowGitHub(LocalGitHub):
    def clone(self, repo, workspace, base):
        super().clone(repo, workspace, base)
        if phase == 'preparing':
            stall(workspace)

class SlowExecutor(FixExecutor):
    def execute(self, workspace, run_dir, spec, project):
        if phase == 'running':
            stall(workspace)
        return super().execute(workspace, run_dir, spec, project)

class SlowValidator(RealCommandValidator):
    def check(self, workspace, run_dir, commands, stage, network=False):
        if phase == 'testing' and stage == 'test':
            stall(workspace)
        return super().check(workspace, run_dir, commands, stage, network)

engine = Engine(config, runner, SlowGitHub(Path(seed), runner), SlowExecutor(), SlowValidator(runner))
try:
    with engine.store.lock():
        engine.execute(task, local_only=True)
finally:
    engine.store.close()
''', encoding="utf-8")
        with (self.root / "controller.log").open("w") as output:
            return subprocess.Popen([
                sys.executable, "-B", str(program), str(self.path), str(self.seed), task_id, phase,
                "1" if native else "0",
            ], cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)

    def child_ready(self, task_id):
        task = self.engine.store.task(task_id)
        if not task["run_id"]:
            return None
        run = self.engine.store.run(task_id)
        marker = Path(run["workspace"]) / "child-ready.json"
        try:
            return json.loads(marker.read_text())
        except (OSError, ValueError):
            return None

    def stop_child(self, child, run_dir):
        if child:
            self.assertNotEqual(child["pgid"], os.getpgrp())
            try:
                os.killpg(child["pgid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
            wait_until(lambda: lease_state(run_dir) == "idle", "child did not release the attempt lease")

    def cleanup_controller(self, process, child, run_dir):
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        process.wait(timeout=5)
        self.stop_child(child, run_dir)


class RecoveryTests(CrashHarness, EngineFixture):
    def test_sigkill_preserves_work_and_blocks_recovery_until_child_stops(self):
        for phase in ("preparing", "running", "testing"):
            with self.subTest(phase=phase):
                task = self.engine.enqueue("example/demo", "Recover " + phase, "Return correct addition.")
                process = self.start_controller(task, phase)
                child, run_dir = None, None
                try:
                    child = wait_until(lambda: self.child_ready(task), "execution child did not start")
                    run = self.engine.store.run(task)
                    run_dir = Path(run["run_dir"])
                    workspace = Path(run["workspace"])
                    self.assertEqual(self.engine.store.task(task)["state"], phase)
                    self.assertEqual(lease_state(run_dir), "active")
                    self.assertTrue(self.engine.store.engine_active())
                    events = self.event_count()
                    inspected = subprocess.run(self.cli("recover"), capture_output=True, text=True, timeout=10)
                    self.assertEqual(inspected.returncode, 0, inspected.stderr)
                    self.assertIn('"engine_active": true', inspected.stdout)
                    denied = subprocess.run(self.cli("recover", task), capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(denied.returncode, 0)
                    self.assertEqual(self.event_count(), events)

                    process.kill()  # Kill only the controller: its independently grouped child survives.
                    process.wait(timeout=5)
                    self.assertFalse(self.engine.store.engine_active())
                    self.assertEqual(lease_state(run_dir), "active")
                    self.assertIn("殘留程序", render(self.engine).read_text())
                    denied = subprocess.run(self.cli("recover", task), capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(denied.returncode, 0)
                    self.assertEqual(self.engine.store.task(task)["state"], phase)
                    with self.assertRaises(BusyError):
                        self.engine.recover(task, legacy=True)
                    other = self.engine.enqueue("example/demo", "Queued behind " + phase, "Wait for recovery.")
                    with self.assertRaises(DispatchBlocked) as blocked:
                        self.engine.execute(other)
                    self.assertEqual(blocked.exception.reason["kind"], "recovery_required")
                    self.assertEqual(self.engine.store.task(other)["state"], "queued")

                    self.stop_child(child, run_dir)
                    events, attempts = self.event_count(), self.engine.store.total_attempts()
                    recovered = subprocess.run(self.cli("recover", task), capture_output=True, text=True, timeout=10)
                    self.assertEqual(recovered.returncode, 0, recovered.stderr)
                    self.assertEqual(self.engine.store.task(task)["state"], "interrupted")
                    self.assertEqual(self.engine.store.task(task)["run_id"], run["id"])
                    self.assertEqual(self.engine.store.total_attempts(), attempts)
                    self.assertEqual(self.event_count(), events + 1)
                    saved = self.engine.store.run(task)
                    for key, value in run.items():
                        self.assertEqual(saved[key], value)
                    self.assertEqual(saved["recovery"]["previous_state"], phase)
                    self.assertEqual((workspace / "partial-work.txt").read_text(), "preserved")
                    self.engine.recover(task)  # A repeated recovery does not invent another event.
                    self.assertEqual(self.event_count(), events + 1)

                    retried = subprocess.run(self.cli("retry", task), capture_output=True, text=True, timeout=10)
                    self.assertEqual(retried.returncode, 0, retried.stderr)
                    replacement = self.engine.execute(task, local_only=True)
                    self.assertNotEqual(replacement["workspace"], run["workspace"])
                    self.assertEqual(self.engine.store.total_attempts(), attempts + 1)
                    self.assertEqual((workspace / "partial-work.txt").read_text(), "preserved")
                    self.assertIn(run["id"], [item["id"] for item in self.engine.store.runs(task)])
                finally:
                    self.cleanup_controller(process, child, run_dir)

    def test_idle_unfinished_attempt_blocks_dispatch_until_explicit_recovery(self):
        task = self.task()
        process = self.start_controller(task)
        child, run_dir = None, None
        try:
            child = wait_until(lambda: self.child_ready(task), "execution child did not start")
            run_dir = Path(self.engine.store.run(task)["run_dir"])
            process.kill()
            process.wait(timeout=5)
            self.stop_child(child, run_dir)
            other = self.engine.enqueue("example/demo", "Wait for explicit recovery", "Return 5.")
            self.assertIn("recover", render(self.engine).read_text())
            with self.assertRaises(DispatchBlocked):
                self.engine.execute(other)
            self.assertEqual(self.executor.calls, 0)
            self.engine.recover(task)
            self.assertIsNone(self.engine.dispatch_blocker())
        finally:
            self.cleanup_controller(process, child, run_dir)

    def test_legacy_unfinished_attempt_is_not_assumed_idle(self):
        task = self.task()
        self.executor.mode = "wrong"
        with self.assertRaises(ApeeError):
            self.engine.execute(task)
        run = self.engine.store.run(task)
        (Path(run["run_dir"]) / "active.lock").unlink()
        self.engine.store.transition(task, "running", run=run)
        self.assertEqual(self.engine.recovery_status()[0]["action"], "inspect_lease")
        with self.assertRaisesRegex(ApeeError, "缺少執行鎖"):
            self.engine.recover(task, legacy=True)
        run.pop("lease_version")  # Reproduce the actual pre-lease storage format.
        self.engine.store.transition(task, "running", run=run)
        events = self.event_count()
        self.assertEqual(self.engine.recovery_status()[0]["action"], "inspect_legacy")
        with self.assertRaisesRegex(ApeeError, "缺少執行鎖"):
            self.engine.recover(task)
        self.assertEqual(self.event_count(), events)
        self.assertEqual(self.engine.store.task(task)["state"], "running")
        self.assertEqual(self.engine.store.run(task), run)
        self.assertEqual(self.engine.dispatch_blocker()["kind"], "recovery_required")
        self.engine.recover(task, legacy=True)
        self.assertEqual(self.engine.store.task(task)["state"], "interrupted")
        self.assertEqual(self.engine.store.run(task)["recovery"]["proof"], "legacy_manually_checked")
        self.assertEqual(lease_state(run["run_dir"]), "missing")

    def test_unreadable_lease_blocks_retry_and_new_dispatch(self):
        task = self.task()
        self.executor.mode = "wrong"
        with self.assertRaises(ApeeError):
            self.engine.execute(task)
        path = Path(self.engine.store.run(task)["run_dir"]) / "active.lock"
        path.unlink()
        path.mkdir()  # A real filesystem error; it cannot be mistaken for an idle lock.
        events = self.event_count()
        self.assertEqual(lease_state(path.parent), "unreadable")
        with self.assertRaises(ApeeError):
            self.engine.retry(task)
        self.assertEqual(self.event_count(), events)
        self.assertEqual(self.engine.dispatch_blocker()["kind"], "recovery_required")

    def test_publishing_recovery_reconciles_without_repeating_development(self):
        task = self.task()
        run = self.engine.execute(task)
        self.github.ambiguous = True
        with self.assertRaises(ApeeError):
            self.engine.publish(task)
        saved, events = self.engine.store.run(task), self.event_count()
        result = self.engine.recover(task)
        self.assertEqual(result["next_action"], "publish")
        self.assertEqual(self.engine.store.task(task)["state"], "publishing")
        self.assertEqual(self.engine.store.run(task), saved)
        self.assertEqual(self.event_count(), events)
        delivered = self.engine.publish(task)
        self.assertEqual(delivered["commit"], run["commit"])
        self.assertEqual(self.github.creates, 1)
        self.assertEqual(self.executor.calls, 1)

    def test_ready_and_delivered_work_is_not_recovered_or_requeued(self):
        task = self.task()
        self.engine.execute(task)
        for state in ("ready", "review", "merged", "closed"):
            self.engine.store.transition(task, state)
            events = self.event_count()
            with self.assertRaises(ApeeError):
                self.engine.recover(task)
            with self.assertRaises(ApeeError):
                self.engine.retry(task)
            self.assertEqual(self.event_count(), events)
            self.assertEqual(self.engine.store.task(task)["state"], state)

    def test_old_attempt_lease_blocks_retry_even_after_a_later_failure(self):
        task = self.task()
        self.executor.mode = "wrong"
        with self.assertRaises(ApeeError):
            self.engine.execute(task)
        old = self.engine.store.run(task)
        self.engine.retry(task)
        with self.assertRaises(ApeeError):
            self.engine.execute(task)
        current = self.engine.store.run(task)
        holder = self.root / "holder-ready"
        code = (
            "from pathlib import Path; import sys,time; "
            f"sys.path.insert(0, {str(ROOT)!r}); "
            "from apee.leases import attempt_lease; "
            "lease=attempt_lease(sys.argv[1], create=False); lease.__enter__(); "
            "Path(sys.argv[2]).write_text('ready'); time.sleep(60)"
        )
        process = subprocess.Popen([sys.executable, "-B", "-c", code, old["run_dir"], str(holder)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_until(holder.exists, "historical lease holder did not start")
            events = self.event_count()
            with self.assertRaises(ApeeError):
                self.engine.retry(task)
            self.assertEqual(self.event_count(), events)
            self.assertEqual(self.engine.store.run(task)["id"], current["id"])
            pending = self.engine.dispatch_blocker()["attempts"]
            self.assertEqual(pending[0]["run_id"], old["id"])
            self.assertFalse(pending[0]["current_attempt"])
            self.assertIn("殘留程序", render(self.engine).read_text())
        finally:
            process.terminate()
            process.wait(timeout=5)
        self.engine.retry(task)
        self.assertEqual(self.engine.store.task(task)["state"], "queued")


class LeaseTests(unittest.TestCase):
    def test_nested_lease_stays_held_until_its_outer_context_closes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            runner = Runner()
            with runner.lease(temporary):
                with runner.lease(temporary):
                    self.assertEqual(lease_state(temporary), "active")
                self.assertEqual(lease_state(temporary), "active")
                with self.assertRaises(BusyError):
                    with Runner().lease(temporary):
                        self.fail("a second runner acquired a live lease")
            self.assertEqual(lease_state(temporary), "idle")

    def test_timeout_and_spawn_failure_do_not_leave_a_live_lease(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            runner = Runner()
            for command in ([sys.executable, "-c", "import time; time.sleep(30)"],
                            [str(Path(temporary) / "missing-program")]):
                with self.subTest(command=command):
                    with self.assertRaises(ApeeError):
                        with runner.lease(temporary):
                            runner.run(command, timeout=0.1, log=Path(temporary) / "command.log")
                    self.assertEqual(lease_state(temporary), "idle")


@unittest.skipUnless(os.environ.get("APEE_NATIVE_TESTS") == "1", "設 APEE_NATIVE_TESTS=1 執行原生 sandbox 驗證")
class NativeRecoveryTests(CrashHarness, EngineFixture):
    def test_native_validation_retains_lease_after_controller_sigkill(self):
        task = self.task()
        process = self.start_controller(task, native=True)
        child, run_dir = None, None
        try:
            child = wait_until(lambda: self.child_ready(task), "native validation child did not start")
            run_dir = Path(self.engine.store.run(task)["run_dir"])
            process.kill()
            process.wait(timeout=5)
            self.assertEqual(lease_state(run_dir), "active")
            with self.assertRaises(BusyError):
                self.engine.recover(task)
            self.stop_child(child, run_dir)
            self.engine.recover(task)
            self.assertEqual(self.engine.store.task(task)["state"], "interrupted")
        finally:
            self.cleanup_controller(process, child, run_dir)
