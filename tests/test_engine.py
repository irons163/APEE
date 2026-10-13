import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from apee.config import ApeeError, Config
from apee.engine import Engine
from apee.process import Runner
from apee.store import Store


class LocalGitHub:
    """Only network boundaries are faked; cloning, commits and tests are real."""
    def __init__(self, seed, runner):
        self.seed, self.runner = seed, runner
        self.issue_data = dict(number=7, title="Fix addition", body="2 + 3 must return 5.",
                               html_url="https://github.com/example/demo/issues/7",
                               updated_at="2026-10-11T00:00:00Z", state="open",
                               labels=[{"name": "apee:ready"}])
        self.pr = None
        self.creates = 0
        self.pushes = 0
        self.ambiguous = False

    def issues(self, project):
        return [dict(self.issue_data)]

    def issue(self, repo, number):
        return dict(self.issue_data)

    def metadata(self, repo):
        return {"default_branch": "main"}

    def clone(self, repo, workspace, base):
        self.runner.run(["git", "clone", "--no-local", str(self.seed), str(workspace)])

    def find_pr(self, repo, branch):
        return self.pr

    def push(self, repo, workspace, branch, commit):
        self.pushes += 1
        self.pushed_commit = commit
        self.pushed_branch = branch

    def create_pr(self, repo, branch, base, title, body_path):
        self.creates += 1
        self.pr = dict(url="https://github.com/example/demo/pull/8", state="OPEN", isDraft=True,
                       headRefOid=self.pushed_commit, headRefName=branch, baseRefName=base)
        if self.ambiguous:
            raise ApeeError("Simulated connection loss after GitHub accepted the PR")
        return self.pr["url"]


class FixExecutor:
    def __init__(self, mode="fix"):
        self.mode = mode
        self.calls = 0

    def execute(self, workspace, run_dir, spec, project):
        self.calls += 1
        if self.mode == "interrupt":
            raise KeyboardInterrupt()
        if self.mode != "no-change":
            operator = "+" if self.mode == "fix" else "*"
            (workspace / "calculator.py").write_text(f"def add(a, b):\n    return a {operator} b\n")
        return {"fixture": True}


class RealCommandValidator:
    def __init__(self, runner):
        self.runner = runner

    def check(self, workspace, run_dir, commands, phase, network=False):
        results = []
        for i, command in enumerate(commands):
            log = run_dir / f"{phase}-{i}.log"
            self.runner.run(command, cwd=workspace, log=log, timeout=5, restricted=True)
            results.append(dict(command=command, passed=True, log=str(log)))
        return results


class EngineFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="apee-test-")
        self.root = Path(self.tmp.name).resolve()
        self.runner = Runner()
        self.seed = self.root / "seed"
        self.seed.mkdir()
        self.runner.git(self.seed, "init", "-b", "main")
        (self.seed / ".gitignore").write_text("__pycache__/\n")
        (self.seed / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
        (self.seed / "test_calculator.py").write_text(
            "import unittest\nfrom calculator import add\n"
            "class TestAddition(unittest.TestCase):\n"
            "    def test_addition(self):\n        self.assertEqual(add(2, 3), 5)\n"
        )
        self.runner.git(self.seed, "add", "--all")
        self.runner.git(self.seed, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
                        "commit", "-m", "Seed failing regression")
        self.seed_head = self.runner.git(self.seed, "rev-parse", "HEAD")
        self.path = self.root / "config.json"
        self.data = dict(version=1, state_dir="state", projects=[dict(
            repo="example/demo", enabled=True, publish_draft=False,
            tests=[[sys.executable, "-B", "-m", "unittest", "discover", "-v"]],
        )])
        self.path.write_text(json.dumps(self.data))
        self.config = Config(self.path)
        self.github = LocalGitHub(self.seed, self.runner)
        self.executor = FixExecutor()
        self.engine = Engine(self.config, self.runner, self.github, self.executor, RealCommandValidator(self.runner))

    def tearDown(self):
        self.engine.store.close()
        self.tmp.cleanup()

    def task(self):
        return self.engine.enqueue("example/demo", "Fix addition", "Return 5 for add(2, 3). Keep negative-number behavior correct.")


class EngineTests(EngineFixture):
    def test_end_to_end_real_checkout_tests_and_review(self):
        task_id = self.task()
        # Confirm the fixture's starting behavior really fails before the executor fixes it.
        with self.assertRaises(ApeeError):
            self.runner.run(self.config.project("example/demo")["tests"][0], cwd=self.seed)
        run = self.engine.execute(task_id)
        self.assertEqual(self.engine.store.task(task_id)["state"], "ready")
        self.assertIn("return a + b", (Path(run["run_dir"]) / "changes.patch").read_text())
        self.assertIn(run["commit"], (Path(run["run_dir"]) / "review.md").read_text())
        self.assertNotEqual(run["commit"], self.seed_head)
        self.assertEqual(self.runner.git(self.seed, "rev-parse", "HEAD"), self.seed_head)
        self.assertEqual(self.github.creates, 0)
        with self.assertRaises(ApeeError):
            self.engine.execute(task_id)
        self.assertEqual(self.executor.calls, 1)

    def test_failed_tests_never_publish(self):
        self.executor.mode = "wrong"
        self.config.projects["example/demo"]["publish_draft"] = True
        task_id = self.task()
        with self.assertRaises(ApeeError):
            self.engine.execute(task_id)
        self.assertEqual(self.engine.store.task(task_id)["state"], "failed")
        with self.assertRaises(ApeeError):
            self.engine.publish(task_id)
        self.assertEqual(self.github.pushes, 0)

    def test_no_change_is_not_a_successful_delivery(self):
        self.executor.mode = "no-change"
        task_id = self.task()
        with self.assertRaisesRegex(ApeeError, "沒有產生"):
            self.engine.execute(task_id)
        self.assertEqual(self.engine.store.task(task_id)["state"], "failed")

    def test_interrupt_is_persisted(self):
        self.executor.mode = "interrupt"
        task_id = self.task()
        with self.assertRaises(KeyboardInterrupt):
            self.engine.execute(task_id)
        self.assertEqual(self.engine.store.task(task_id)["state"], "interrupted")

    def test_sync_deduplicates_across_restart(self):
        self.assertEqual(len(self.engine.sync()["added"]), 1)
        other = Store(self.config.state_dir)
        try:
            self.assertEqual(other.tasks()[0]["state"], "queued")
        finally:
            other.close()
        self.assertEqual(self.engine.sync()["added"], [])

    def test_manual_task_deduplicates_and_preserves_history(self):
        first = self.task()
        self.assertEqual(self.task(), first)
        self.engine.execute(first)
        self.assertEqual(self.task(), first)
        self.assertEqual(self.engine.store.task(first)["state"], "ready")

    def test_removed_issue_label_stops_before_execution(self):
        self.engine.sync()
        self.github.issue_data["labels"] = []
        with self.assertRaisesRegex(ApeeError, "移除派工標籤"):
            self.engine.execute("example/demo#7")
        self.assertEqual(self.executor.calls, 0)

    def test_latest_issue_body_is_frozen_in_run(self):
        self.engine.sync()
        self.github.issue_data["body"] = "Updated acceptance: 2 + 3 = 5."
        run = self.engine.execute("example/demo#7")
        self.assertEqual(run["spec"]["body"], self.github.issue_data["body"])

    def test_disabled_repo_never_runs(self):
        task_id = self.task()
        self.config.projects["example/demo"]["enabled"] = False
        self.assertIsNone(self.engine.next_task())
        with self.assertRaises(ApeeError):
            self.engine.execute(task_id)
        self.assertEqual(self.executor.calls, 0)

    def test_narrow_validation_cannot_publish_changes_outside_its_scope(self):
        self.config.projects["example/demo"]["allowed_paths"] = ["test_calculator.py"]
        task_id = self.task()
        with self.assertRaisesRegex(ApeeError, "超出設定的工作範圍"):
            self.engine.execute(task_id)
        self.assertEqual(self.engine.store.task(task_id)["state"], "failed")
        self.assertEqual(self.github.pushes, 0)

    def test_ambiguous_pr_creation_reconciles_without_duplicate(self):
        task_id = self.task()
        self.engine.execute(task_id)
        self.github.ambiguous = True
        with self.assertRaises(ApeeError):
            self.engine.publish(task_id)
        self.assertEqual(self.engine.store.task(task_id)["state"], "publishing")
        run = self.engine.publish(task_id)
        self.assertEqual(run["pr_url"], self.github.pr["url"])
        self.assertEqual(self.engine.store.task(task_id)["state"], "review")
        self.assertEqual(self.github.creates, 1)
        self.assertEqual(self.github.pushes, 1)
        self.engine.publish(task_id)
        self.assertEqual(self.github.creates, 1)

    def test_modified_checkout_cannot_be_published(self):
        task_id = self.task()
        run = self.engine.execute(task_id)
        (Path(run["workspace"]) / "calculator.py").write_text("tampered\n")
        with self.assertRaisesRegex(ApeeError, "已變更"):
            self.engine.publish(task_id)
        self.assertEqual(self.github.pushes, 0)

    def test_existing_pr_with_wrong_commit_is_not_reused(self):
        task_id = self.task()
        run = self.engine.execute(task_id)
        self.github.pr = dict(headRefOid="wrong", baseRefName=run["base"], state="OPEN")
        with self.assertRaisesRegex(ApeeError, "版本"):
            self.engine.publish(task_id)
        self.assertEqual(self.github.pushes, 0)

    def test_issue_changed_during_execution_is_not_published(self):
        self.engine.sync()
        self.engine.execute("example/demo#7")
        self.github.issue_data["updated_at"] = "2026-10-11T01:00:00Z"
        with self.assertRaisesRegex(ApeeError, "已變更或取消派工"):
            self.engine.publish("example/demo#7")
        self.assertEqual(self.github.pushes, 0)

    def test_tests_cannot_rewrite_verified_source(self):
        self.config.projects["example/demo"]["tests"].append([
            sys.executable, "-c", "from pathlib import Path; Path('calculator.py').write_text('changed')",
        ])
        task_id = self.task()
        with self.assertRaisesRegex(ApeeError, "驗證版本不一致"):
            self.engine.execute(task_id)

    def test_engine_lock_prevents_two_workers(self):
        other = Store(self.config.state_dir)
        try:
            with self.engine.store.lock():
                with self.assertRaises(ApeeError):
                    with other.lock():
                        self.fail("second engine entered")
        finally:
            other.close()

    def test_status_can_be_read_while_engine_is_running(self):
        import subprocess
        task_id = self.task()
        with self.engine.store.lock():
            result = subprocess.run([sys.executable, "-B", "-m", "apee", "--config", str(self.path), "status"],
                                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(task_id, result.stdout)

    def test_cycle_obeys_task_limit_and_local_only(self):
        self.config.projects["example/demo"]["publish_draft"] = True
        self.task()
        self.engine.enqueue("example/demo", "Second task", "Fix addition and document it.")
        result = self.engine.cycle(local_only=True)
        self.assertEqual(len(result["runs"]), 1)
        self.assertEqual(self.executor.calls, 1)
        self.assertEqual(self.github.creates, 0)
        self.assertIsNotNone(self.engine.next_task())

    def test_auto_publish_requires_passing_tests(self):
        self.config.projects["example/demo"]["publish_draft"] = True
        task_id = self.task()
        run = self.engine.execute(task_id)
        self.assertEqual(self.engine.store.task(task_id)["state"], "review")
        self.assertEqual(run["commit"], self.github.pushed_commit)

    def test_imported_patch_uses_same_validation_and_records_provenance(self):
        (self.seed / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
        patch = self.root / "external.patch"
        self.runner.git(self.seed, "diff", "--binary", log=patch)
        self.runner.git(self.seed, "restore", "calculator.py")
        task_id = self.task()
        run = self.engine.execute(task_id, patch=patch)
        self.assertEqual(self.executor.calls, 0)
        self.assertEqual(run["agent"]["backend"], "imported_patch")
        self.assertEqual(self.engine.store.task(task_id)["state"], "ready")
        self.assertIn("外部提供的 patch", (Path(run["run_dir"]) / "review.md").read_text())

    def test_bad_imported_patch_cannot_skip_tests(self):
        (self.seed / "calculator.py").write_text("def add(a, b):\n    return a * b\n")
        patch = self.root / "external.patch"
        self.runner.git(self.seed, "diff", "--binary", log=patch)
        self.runner.git(self.seed, "restore", "calculator.py")
        task_id = self.task()
        with self.assertRaises(ApeeError):
            self.engine.execute(task_id, patch=patch)
        self.assertEqual(self.engine.store.task(task_id)["state"], "failed")
        self.assertEqual(self.github.pushes, 0)


class ConfigurationTests(unittest.TestCase):
    def test_config_dir_commands_are_portable_and_preserve_raw_data(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            config_dir = base / "settings with spaces"
            config_dir.mkdir()
            checkout = base / "checkout"
            checkout.mkdir()
            script = config_dir / "check.py"
            script.write_text(
                "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            ordinary = ["relative/file", "/absolute/file", "$HOME", "${HOME}",
                        "~/file", "*.py", "$(echo expanded)", "{other}"]
            command = [sys.executable, "{config_dir}/check.py",
                       "prefix={config_dir}|{config_dir}"] + ordinary
            data = dict(version=1, projects=[dict(
                repo="me/demo", enabled=True, tests=[command], setup=[command],
            )])
            path = config_dir / "config.json"
            original = json.dumps(data)
            path.write_text(original, encoding="utf-8")
            previous_cwd = Path.cwd()
            try:
                for cwd in (base, checkout):
                    with self.subTest(cwd=cwd):
                        os.chdir(cwd)
                        with patch.dict(os.environ, HOME="should-not-expand"):
                            config = Config(os.path.relpath(path, cwd))
                        expected = [sys.executable, str(script),
                                    f"prefix={config_dir}|{config_dir}"] + ordinary
                        project = config.project("me/demo")
                        for key in ("tests", "setup"):
                            self.assertEqual(project[key], [expected])
                            result = Runner().run(project[key][0], cwd=checkout)
                            self.assertEqual(json.loads(result), expected[2:])
                            project[key][0].append("runtime-only")
                        self.assertEqual(config.data, data)
                        self.assertEqual(json.loads(json.dumps(config.data)), data)
                        self.assertEqual(path.read_text(encoding="utf-8"), original)
            finally:
                os.chdir(previous_cwd)

    def test_config_dir_does_not_bypass_command_validation(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            for key in ("tests", "setup"):
                for invalid in ("{config_dir}/check.py", [[]],
                                [["{config_dir}", ""]], [["{config_dir}", 1]],
                                [["{config_dir}\x00"]]):
                    with self.subTest(key=key, invalid=invalid):
                        project = dict(repo="me/demo", enabled=True, tests=[["python3"]])
                        project[key] = invalid
                        path.write_text(json.dumps(dict(version=1, projects=[project])))
                        with self.assertRaises(ApeeError):
                            Config(path)

    def test_enabled_repository_requires_tests(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text(json.dumps(dict(version=1, projects=[dict(repo="me/demo", enabled=True)])))
            with self.assertRaises(ApeeError):
                Config(path)

    def test_string_commands_and_path_traversal_are_rejected(self):
        from apee.config import commands, repo_name
        for value in ("npm test", ["npm test"], [[]]):
            with self.assertRaises(ApeeError):
                commands(value, "tests", required=True)
        for name in ("../repo", "owner/../../repo", "-R/evil", "https://github.com/me/repo"):
            with self.assertRaises(ApeeError):
                repo_name(name)


class ProcessTests(unittest.TestCase):
    def test_timeout_stops_child_processes(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "child-survived"
            child = "import time; from pathlib import Path; time.sleep(2); Path(" + repr(str(marker)) + ").write_text('bad')"
            parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(5)"
            with self.assertRaisesRegex(ApeeError, "超過"):
                Runner().run([sys.executable, "-c", parent], timeout=0.1, log=Path(root) / "timeout.log")
            # The group is killed synchronously; wait past the child's scheduled write to prove it.
            import time
            time.sleep(2.1)
            self.assertFalse(marker.exists())

    def test_restricted_environment_does_not_inherit_api_tokens(self):
        from unittest.mock import patch
        from apee.process import limited_env
        with patch.dict(os.environ, GH_TOKEN="fixture-secret", OPENAI_API_KEY="fixture-secret"):
            env = limited_env()
            self.assertNotIn("GH_TOKEN", env)
            self.assertNotIn("OPENAI_API_KEY", env)


if __name__ == "__main__":
    unittest.main()
