import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from apee.config import ApeeError, Config
from apee.executors import CodexExecutor, ExecutorUnavailable, Validator
from apee.github import GitHub
from apee.process import Runner


class ScriptedRunner:
    def __init__(self, events, fail=False):
        self.events = events
        self.fail = fail
        self.args = None

    def run(self, args, **kwargs):
        self.args = args
        kwargs["log"].write_text("\n".join(json.dumps(e) for e in self.events))
        if self.fail:
            raise ApeeError("fixture process exited with code 1")
        return ""


class ExecutorTests(unittest.TestCase):
    def test_provider_errors_are_classified_even_when_process_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.json"
            path.write_text('{"version":1}')
            cases = [
                ("usage_limit", {"message": "You’ve hit your usage limit. Try again at 2:21 PM."}),
                ("usage_limit", {"code": "insufficient_quota"}),
                ("authentication", {"message": "Please log in again."}),
                ("authentication", {"code": "invalid_api_key"}),
                ("rate_limit", {"code": "rate_limit_exceeded"}),
            ]
            for kind, error in cases:
                for fail in (True, False):
                    with self.subTest(kind=kind, error=error, fail=fail):
                        runner = ScriptedRunner([{"type": "turn.failed", "error": error}], fail=fail)
                        with self.assertRaises(ExecutorUnavailable) as raised:
                            CodexExecutor(Config(path), runner).execute(root, root, {}, {"tests": [["true"]]})
                        self.assertEqual(raised.exception.reason["kind"], kind)
                        self.assertEqual(raised.exception.reason["log"], str(root / "agent-events.log"))

    def test_task_and_command_output_cannot_be_mistaken_for_provider_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.json"
            path.write_text('{"version":1}')
            runner = ScriptedRunner([
                {"type": "item.completed", "item": {"aggregated_output": "usage limit"}},
                {"type": "turn.completed"},
            ])
            result = CodexExecutor(Config(path), runner).execute(root, root, {"body": "Fix usage limit UI"}, {"tests": [["true"]]})
            self.assertEqual(result["backend"], "codex")

    def test_unknown_command_failure_is_not_a_global_provider_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.json"
            path.write_text('{"version":1}')
            runner = ScriptedRunner([{"type": "error", "message": "invalid config"}], fail=True)
            with self.assertRaises(ApeeError) as raised:
                CodexExecutor(Config(path), runner).execute(root, root, {}, {"tests": [["true"]]})
            self.assertNotIsInstance(raised.exception, ExecutorUnavailable)
            self.assertIn("exited with code 1", str(raised.exception))

    def test_exit_zero_without_completed_event_is_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.json"
            path.write_text('{"version":1}')
            for events in ([], [{"type": "turn.failed"}], [{"type": "turn.completed"}, {"type": "error"}]):
                runner = ScriptedRunner(events)
                executor = CodexExecutor(Config(path), runner)
                with self.assertRaises(ApeeError):
                    executor.execute(root, root, {"title": "fixture"}, {"tests": [["python3", "test.py"]]})

    def test_completed_event_preserves_usage_and_restricted_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.json"
            path.write_text('{"version":1}')
            runner = ScriptedRunner([{"type": "turn.completed", "usage": {"input_tokens": 123}}])
            result = CodexExecutor(Config(path), runner).execute(root, root, {"title": "fixture"}, {"tests": [["true"]]})
            self.assertEqual(result["usage"][0]["input_tokens"], 123)
            self.assertIn("workspace-write", runner.args)
            self.assertIn('approval_policy="never"', runner.args)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", runner.args)


class GithubTests(unittest.TestCase):
    def test_paginated_issues_exclude_pull_requests(self):
        from unittest.mock import Mock
        runner = Mock()
        runner.run.return_value = json.dumps([
            [{"number": 1}, {"number": 2, "pull_request": {}}], [{"number": 3}],
        ])
        issues = GitHub(runner).issues({"repo": "example/demo", "issue_label": "apee:ready"})
        self.assertEqual([i["number"] for i in issues], [1, 3])
        args = runner.run.call_args.args[0]
        self.assertIn("--paginate", args)
        self.assertIn("--slurp", args)
        self.assertEqual(args[args.index("--method") + 1], "GET")

    def test_remote_branch_with_different_commit_cannot_be_overwritten(self):
        from unittest.mock import Mock
        runner = Mock()
        runner.git.return_value = "different refs/heads/apee/task-abc"
        with self.assertRaises(ApeeError):
            GitHub(runner).push("example/demo", Path("/unused"), "apee/task-abc", "expected")
        self.assertEqual(runner.git.call_count, 1)


@unittest.skipUnless(os.environ.get("APEE_NATIVE_TESTS") == "1", "設 APEE_NATIVE_TESTS=1 執行原生 sandbox 驗證")
class NativeSandboxTests(unittest.TestCase):
    def test_validation_allows_checkout_writes_and_denies_parent_writes(self):
        # A directory outside system temp is essential: :workspace allows temp writes.
        with tempfile.TemporaryDirectory(prefix=".native-test-", dir=Path(__file__).resolve().parents[1]) as tmp:
            root = Path(tmp).resolve()
            work = root / "repo"
            work.mkdir()
            path = root / "config.json"
            path.write_text('{"version":1}')
            code = (
                "from pathlib import Path\n"
                "Path('inside').write_text('ok')\n"
                "try:\n    Path(" + repr(str(root / "outside")) + ").write_text('bad')\n"
                "except PermissionError:\n    pass\n"
                "else:\n    raise RuntimeError('sandbox allowed an outside write')\n"
            )
            results = Validator(Config(path), Runner()).check(work, root, [[sys.executable, "-c", code]], "native")
            self.assertTrue(results[0]["passed"])
            self.assertEqual((work / "inside").read_text(), "ok")
            self.assertFalse((root / "outside").exists())

    def test_tests_are_offline_and_setup_network_is_explicit(self):
        import socket
        with tempfile.TemporaryDirectory(prefix=".native-test-", dir=Path(__file__).resolve().parents[1]) as tmp:
            root = Path(tmp).resolve()
            work = root / "repo"
            work.mkdir()
            path = root / "config.json"
            path.write_text('{"version":1}')
            with socket.socket() as server:
                server.bind(("127.0.0.1", 0))
                server.listen(2)
                port = server.getsockname()[1]
                code = f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=1).close()"
                validator = Validator(Config(path), Runner())
                with self.assertRaises(ApeeError):
                    validator.check(work, root, [[sys.executable, "-c", code]], "offline")
                result = validator.check(work, root, [[sys.executable, "-c", code]], "setup", network=True)
                self.assertTrue(result[0]["passed"])


if __name__ == "__main__":
    unittest.main()
