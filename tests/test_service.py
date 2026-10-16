import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from apee.cli import main
from apee.config import ApeeError, BusyError, Config
from apee.service import Service
from apee.store import Store


class FakeLaunchd:
    def __init__(self):
        self.jobs, self.calls, self.disabled = {}, [], set()
        self.fail, self.ambiguous = None, False
        self.list_output = None
        self.manager = "Aqua"
        self.actions, self.probes, self.probe_error = [], {}, None
        self.probe_fail = None

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        if args[0] == "/usr/bin/env":
            if self.fail == "auth" and "auth" in args:
                raise ApeeError("fixture-secret must not be echoed")
            if "sandbox" in args:
                return "--permission-profile --include-managed-config"
            if "exec" in args:
                return "--ignore-user-config --output-schema --output-last-message"
            return "tool 1.0"
        if args[0] != "/bin/launchctl":
            raise AssertionError(args)
        action = args[1]
        if action == "managername":
            return self.manager
        if action == "manageruid":
            return str(os.getuid())
        if action == "list":
            if self.list_output is not None:
                return self.list_output
            lines = ["PID\tStatus\tLabel", "-\t0\tcom.example.other job"]
            for label, job in self.jobs.items():
                lines.append(f"{job['pid'] or '-'}\t{job['exit']}\t{label}")
            return "\n".join(lines) + "\n"
        label = args[2].split("/")[-1]
        if action == "bootstrap":
            definition = plistlib.loads(Path(args[3]).read_bytes())
            label = definition["Label"]
        if ".preflight." in label:
            if action == "bootstrap":
                self.probes[label] = definition
                self.jobs[label] = dict(pid=None, exit=0)
                if self.probe_fail == "bootstrap":
                    raise ApeeError("probe response lost after registration")
            elif action == "kickstart":
                result = dict(ok=False, stage=self.probe_error) if self.probe_error else dict(ok=True)
                Path(self.probes[label]["StandardOutPath"]).write_text(json.dumps(result))
            elif action == "bootout":
                if self.probe_fail == "bootout":
                    raise ApeeError("probe cleanup failed")
                self.jobs.pop(label, None)
                self.probes.pop(label, None)
            else:
                raise AssertionError(args)
            return ""
        self.actions.append(action)
        if action == self.fail and not (action == "bootstrap" and self.ambiguous):
            raise ApeeError("fixture launchctl failure")
        if action == "enable":
            self.disabled.discard(label)
        elif action == "disable":
            self.disabled.add(label)
        elif action == "bootstrap":
            definition = plistlib.loads(Path(args[3]).read_bytes())
            label = definition["Label"]
            if label in self.jobs:
                raise ApeeError("already loaded")
            self.jobs[label] = dict(pid=None, exit=0)
            if self.fail == "bootstrap":
                raise ApeeError("fixture response lost after registration")
        elif action == "kickstart":
            self.jobs[label]["pid"] = 12345
        elif action == "bootout":
            del self.jobs[label]
        else:
            raise AssertionError(args)
        return ""

    def count(self, action):
        return self.actions.count(action)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="apee-service-")
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "settings with spaces" / "apee.json"
        self.path.parent.mkdir()
        self.data = dict(version=1, state_dir=str(self.root / "state"), projects=[])
        self.path.write_text(json.dumps(self.data))
        self.runner = FakeLaunchd()
        self.service = self.manager(self.path)
        self.platform = patch("apee.service.sys.platform", "darwin")
        self.tools = patch("apee.service.shutil.which", side_effect=lambda name, **_: "/tools/" + Path(name).name)
        self.platform.start()
        self.tools.start()

    def tearDown(self):
        self.tools.stop()
        self.platform.stop()
        self.temporary.cleanup()

    def manager(self, path):
        return Service(path, self.runner, self.root / "Launch Agents", self.root / "registry")

    def test_render_has_bounded_arguments_and_no_registration_side_effects(self):
        output = self.root / "preview with spaces.plist"
        result = self.service.render(output, interval=600, max_tasks=2, local_only=True)
        definition = plistlib.loads(output.read_bytes())
        args = definition["ProgramArguments"]
        self.assertEqual(args[:2], ["/usr/bin/env", "-i"])
        self.assertEqual(args[args.index("--config") + 1], str(self.path))
        self.assertEqual(args[args.index("--max-cycles") + 1], "1")
        self.assertEqual(args[args.index("--max-tasks") + 1], "2")
        self.assertEqual(args[args.index("--expected-state-dir") + 1], self.data["state_dir"])
        self.assertIn("--local-only", args)
        self.assertFalse(definition["RunAtLoad"])
        self.assertFalse(definition["KeepAlive"])
        self.assertEqual(definition["StartInterval"], 600)
        self.assertEqual(definition["ExitTimeOut"], 30)
        self.assertEqual(definition["Umask"], 0o077)
        self.assertEqual(result["definition"], definition)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.service.agents_dir.exists())
        self.assertFalse(self.service.registry_dir.exists())
        self.assertFalse(Config(self.path).state_dir.exists())
        self.assertEqual(self.runner.calls, [])
        self.service.render(output, interval=600, max_tasks=2, local_only=True)

    def test_environment_is_cleared_and_credentials_are_not_persisted(self):
        with patch.dict(os.environ, GH_TOKEN="fixture-secret", GITHUB_TOKEN="fixture-secret",
                        OPENAI_API_KEY="fixture-secret", PYTHONPATH="fixture-secret",
                        DYLD_LIBRARY_PATH="fixture-secret", PATH="relative::/usr/bin"):
            definition = self.service.definition()
        self.assertNotIn("fixture-secret", json.dumps(definition))
        env = definition["EnvironmentVariables"]
        self.assertTrue(all(Path(part).is_absolute() for part in env["PATH"].split(os.pathsep)))
        self.assertIn(str(Path.home() / ".local/bin"), env["PATH"].split(os.pathsep))
        self.assertEqual(definition["ProgramArguments"][:2], ["/usr/bin/env", "-i"])
        self.assertEqual(env["GH_PROMPT_DISABLED"], "1")

    def test_invalid_options_and_unknown_preview_file_are_rejected(self):
        for options in (dict(interval=59), dict(interval=True), dict(interval=86401),
                        dict(max_tasks=0), dict(max_tasks=1001), dict(local_only="yes")):
            with self.subTest(options=options), self.assertRaises(ApeeError):
                self.service.render(**options)
        output = self.root / "existing.plist"
        output.write_text("preserve")
        with self.assertRaises(ApeeError):
            self.service.render(output)
        self.assertEqual(output.read_text(), "preserve")

    def test_install_start_stop_and_uninstall_preserve_task_data(self):
        installed = self.service.install()
        self.assertTrue(installed["owned"] and installed["installed"] and installed["loaded"])
        self.assertFalse(installed["running"])
        self.assertEqual(self.runner.count("kickstart"), 0)
        self.assertEqual(self.service.plist_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.service.record_path.stat().st_mode & 0o777, 0o600)
        self.service.install()
        self.assertEqual(self.runner.count("bootstrap"), 1)
        state = Config(self.path).state_dir
        store = Store(state)
        store.set_control("paused", {"reason": "preserve"})
        store.close()
        (state / "service/stdout.log").write_text("keep log")
        self.assertTrue(self.service.start()["running"])
        self.service.start()
        self.assertEqual(self.runner.count("kickstart"), 1)
        self.assertFalse(self.service.stop()["loaded"])
        self.assertIn(self.service.label, self.runner.disabled)
        self.assertTrue(self.service.start()["loaded"])
        self.assertNotIn(self.service.label, self.runner.disabled)
        removed = self.service.uninstall()
        self.assertFalse(removed["owned"] or removed["installed"] or removed["loaded"])
        self.assertEqual((state / "service/stdout.log").read_text(), "keep log")
        store = Store(state)
        self.assertEqual(store.control("paused"), {"reason": "preserve"})
        store.close()
        self.service.uninstall()

    def test_bootstrap_failure_retains_ownership_for_retry(self):
        self.runner.fail = "bootstrap"
        with self.assertRaises(ApeeError):
            self.service.install()
        self.assertTrue(self.service.plist_path.exists())
        self.assertTrue(self.service.record_path.exists())
        self.assertTrue(self.service.status()["owned"])
        self.runner.fail = None
        self.assertTrue(self.service.start()["loaded"])
        self.service.uninstall()

    def test_ambiguous_bootstrap_does_not_register_duplicate_jobs(self):
        self.runner.fail, self.runner.ambiguous = "bootstrap", True
        with self.assertRaises(ApeeError):
            self.service.install()
        self.runner.fail = None
        self.assertTrue(self.service.install()["loaded"])
        self.assertEqual(self.runner.count("bootstrap"), 1)
        self.assertEqual(len(self.runner.jobs), 1)

    def test_failed_bootout_keeps_files_and_can_be_retried(self):
        self.service.install()
        self.runner.fail = "bootout"
        with self.assertRaises(ApeeError):
            self.service.uninstall()
        self.assertTrue(self.service.plist_path.exists() and self.service.record_path.exists())
        self.assertTrue(self.service.status()["loaded"])
        self.runner.fail = None
        self.assertFalse(self.service.uninstall()["loaded"])

    def test_unknown_or_modified_jobs_are_not_overwritten_or_removed(self):
        self.runner.jobs[self.service.label] = dict(pid=None, exit=0)
        for action in (self.service.install, self.service.stop, self.service.uninstall):
            with self.assertRaises(ApeeError):
                action()
        self.assertEqual(self.runner.count("bootout"), 0)
        del self.runner.jobs[self.service.label]
        self.service.plist_path.parent.mkdir(parents=True)
        self.service.plist_path.write_text("foreign file")
        with self.assertRaises(ApeeError):
            self.service.install()
        self.assertEqual(self.service.plist_path.read_text(), "foreign file")
        self.service.plist_path.unlink()
        self.service.install()
        self.service.plist_path.write_text("modified after install")
        for action in (self.service.start, self.service.stop, self.service.uninstall):
            with self.assertRaises(ApeeError):
                action()
        self.assertEqual(self.runner.count("bootout"), 0)
        self.assertEqual(self.service.plist_path.read_text(), "modified after install")

    def test_invalid_record_and_symbolic_links_are_not_taken_over(self):
        self.service.registry_dir.mkdir()
        self.service.record_path.write_text("[]")
        with self.assertRaises(ApeeError):
            self.service.install()
        self.service.record_path.unlink()
        foreign = self.root / "foreign"
        foreign.write_text("preserve")
        self.service.record_path.symlink_to(foreign)
        with self.assertRaises(ApeeError):
            self.service.uninstall()
        self.assertEqual(foreign.read_text(), "preserve")

    def test_two_configs_cannot_schedule_the_same_state_directory(self):
        self.service.install()
        other_path = self.path.with_name("second.json")
        other_path.write_text(json.dumps(self.data))
        other = self.manager(other_path)
        self.assertNotEqual(other.label, self.service.label)
        with self.assertRaisesRegex(ApeeError, "同一份狀態"):
            other.install()
        self.assertFalse(other.record_path.exists())
        self.assertEqual(len(self.runner.jobs), 1)

    def test_changed_state_directory_is_rejected_before_new_state_or_work(self):
        self.service.install()
        definition = plistlib.loads(self.service.plist_path.read_bytes())
        self.data["state_dir"] = str(self.root / "new-state")
        self.path.write_text(json.dumps(self.data))
        self.assertTrue(self.service.status()["needs_reinstall"])
        with self.assertRaises(ApeeError):
            self.service.start()
        result = subprocess.run(definition["ProgramArguments"], cwd=definition["WorkingDirectory"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("state_dir", result.stderr)
        self.assertFalse(Path(self.data["state_dir"]).exists())
        self.assertFalse(self.service.stop()["loaded"])
        self.service.uninstall()

    def test_missing_config_or_missing_plist_can_still_be_stopped_and_removed(self):
        self.service.install()
        self.path.unlink()
        self.assertTrue(self.service.status()["needs_reinstall"])
        with patch("apee.cli.Service", return_value=self.service), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(self.path), "service", "stop"]), 0)
        self.service.plist_path.unlink()
        self.assertFalse(self.service.uninstall()["owned"])

    def test_different_settings_require_explicit_reinstall(self):
        self.service.install()
        original = self.service.plist_path.read_bytes()
        with self.assertRaises(ApeeError):
            self.service.install(interval=600)
        self.assertEqual(self.service.plist_path.read_bytes(), original)
        self.assertEqual(self.runner.count("bootstrap"), 1)

    def test_preflight_uses_only_planned_environment_and_hides_auth_output(self):
        self.runner.fail = "auth"
        with patch.dict(os.environ, GH_TOKEN="fixture-secret"):
            with self.assertRaises(ApeeError) as failed:
                self.service.install()
        self.assertNotIn("fixture-secret", str(failed.exception))
        self.assertFalse(self.service.record_path.exists() or self.service.plist_path.exists())
        checks = [args for args in self.runner.calls if args[0] == "/usr/bin/env"]
        self.assertTrue(checks)
        self.assertTrue(all(args[:2] == ["/usr/bin/env", "-i"] for args in checks))
        self.assertNotIn("fixture-secret", json.dumps(checks))

    def test_background_access_failure_does_not_install_and_cleans_probe(self):
        self.runner.probe_error = "source"
        with self.assertRaisesRegex(ApeeError, "launchd 無法存取"):
            self.service.install()
        self.assertFalse(self.service.record_path.exists() or self.service.plist_path.exists())
        self.assertEqual(self.runner.jobs, {})
        self.assertEqual(self.runner.probes, {})
        self.assertEqual(list(self.service.registry_dir.glob("access-check-*")), [])
        self.assertEqual(self.runner.count("bootstrap"), 0)

    def test_ambiguous_probe_registration_is_cleaned_before_returning(self):
        self.runner.probe_fail = "bootstrap"
        with self.assertRaises(ApeeError):
            self.service.install()
        self.assertEqual(self.runner.jobs, {})
        self.assertEqual(list(self.service.registry_dir.glob("access-check-*")), [])
        self.assertFalse(self.service.record_path.exists() or self.service.plist_path.exists())

    def test_failed_probe_cleanup_keeps_definition_and_reports_target(self):
        self.runner.probe_fail = "bootout"
        with self.assertRaises(ApeeError) as failed:
            self.service.install()
        self.assertIn("背景存取檢查無法完成清理", str(failed.exception))
        self.assertEqual(len(self.runner.probes), 1)
        label, definition = next(iter(self.runner.probes.items()))
        self.assertIn(label, str(failed.exception))
        root = Path(definition["StandardOutPath"]).parent
        self.assertTrue((root / "probe.plist").exists())
        self.assertFalse(self.service.record_path.exists() or self.service.plist_path.exists())
        self.runner.probe_fail = None
        self.service._launchctl("bootout", self.service.domain + "/" + label)
        shutil.rmtree(root)

    def test_management_lock_and_unknown_launchctl_output_fail_closed(self):
        with self.service._lock():
            with self.assertRaises(BusyError):
                self.manager(self.path).install()
        self.runner.list_output = "unexpected new output"
        with self.assertRaises(ApeeError):
            self.service.install()
        self.assertFalse(self.service.record_path.exists())
        self.assertEqual(self.runner.count("bootstrap"), 0)

    def test_non_gui_context_and_non_macos_are_rejected(self):
        self.runner.manager = "Background"
        with self.assertRaises(ApeeError):
            self.service.install()
        with patch("apee.service.sys.platform", "linux"):
            with self.assertRaises(ApeeError):
                self.service.status()
        self.assertFalse(self.service.record_path.exists() or self.service.plist_path.exists())

    def test_non_gui_stop_does_not_change_enabled_state(self):
        self.service.install()
        self.runner.manager = "Background"
        with self.assertRaises(ApeeError):
            self.service.stop()
        self.assertEqual(self.runner.count("disable"), 0)
        self.assertNotIn(self.service.label, self.runner.disabled)


@unittest.skipUnless(os.environ.get("APEE_NATIVE_TESTS") == "1" and sys.platform == "darwin",
                     "設 APEE_NATIVE_TESTS=1 執行原生 launchd 驗證")
class NativeServiceTests(unittest.TestCase):
    def test_real_launchd_runs_one_empty_cycle_and_is_removed(self):
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="apee-native-service-", dir=cache) as temporary:
            root = Path(temporary).resolve()
            path = root / "config with spaces.json"
            path.write_text(json.dumps(dict(version=1, state_dir="state", projects=[])))
            service = Service(path, registry_dir=root / "registry")
            # launchd may not read a development checkout under Documents.
            # Keep this test's runtime, config and state in a disposable cache.
            service.source_root = root / "runtime"
            shutil.copytree(Path(__file__).resolve().parents[1] / "apee", service.source_root / "apee",
                            ignore=shutil.ignore_patterns("__pycache__"))
            try:
                preview = root / "preview.plist"
                service.render(preview, interval=60, max_tasks=1, local_only=True)
                subprocess.run(["/usr/bin/plutil", "-lint", str(preview)], check=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                result = service.install(interval=60, max_tasks=1, local_only=True)
                self.assertTrue(result["loaded"])
                self.assertFalse(result["running"])
                self.assertFalse((root / "state/state.sqlite3").exists())
                service.start()
                deadline, worker = time.monotonic() + 8, None
                while time.monotonic() < deadline:
                    if (root / "state/state.sqlite3").exists():
                        store = Store(root / "state")
                        try:
                            worker = store.control("worker")
                            if worker and worker["state"] == "stopped":
                                self.assertEqual(store.total_attempts(), 0)
                                break
                        finally:
                            store.close()
                    time.sleep(0.1)
                else:
                    logs = Path(result["stderr"]).read_text() if Path(result["stderr"]).exists() else "no stderr"
                    self.fail("background cycle did not complete: " + logs)
                self.assertEqual(worker["cycles"], 1)
                self.assertEqual(worker["attempts"], 0)
                self.assertEqual(worker["stop_reason"], "cycle_limit")
                self.assertFalse(service.stop()["loaded"])
                path.unlink()  # Removal still works after the original configuration is gone.
                self.assertFalse(service.uninstall()["installed"])
                self.assertTrue((root / "state/state.sqlite3").exists())
                self.assertTrue(Path(result["stdout"]).exists())
            finally:
                try:
                    service.uninstall()
                finally:
                    subprocess.run(["/bin/launchctl", "bootout", service.target],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                    subprocess.run(["/bin/launchctl", "enable", service.target],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                    service.plist_path.unlink(missing_ok=True)
                    self.assertFalse(service._job()["loaded"], "temporary native service was not removed")
