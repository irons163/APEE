"""Model execution and independent validation adapters."""

import json
from pathlib import Path

from .config import ApeeError


class ExecutorUnavailable(ApeeError):
    """An actionable provider failure; retry requires an explicit resume."""

    def __init__(self, kind, log):
        messages = {
            "usage_limit": "Codex 用量已達上限；確認額度可用後，以 resume --codex 恢復派工。",
            "rate_limit": "Codex 暫時限制請求；確認服務恢復後，以 resume --codex 恢復派工。",
            "authentication": "Codex 登入失效；重新登入後，以 resume --codex 恢復派工。",
        }
        self.reason = {"kind": kind, "message": messages[kind], "log": str(log)}
        super().__init__(messages[kind])


def read_events(log):
    """Only structured provider errors are classified, never command output or task text."""
    completed, failed, usage, kind = False, False, [], None
    if not log.exists():
        return completed, failed, usage, kind
    with log.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "turn.completed":
                completed = True
                usage.append(event.get("usage", {}))
            if event.get("type") not in ("turn.failed", "error"):
                continue
            failed = True
            text = json.dumps(event, ensure_ascii=False).lower()
            if any(token in text for token in (
                "usage limit", "usage_limit", "insufficient_quota", "quota exhausted", "exceeded your current quota",
            )):
                kind = "usage_limit"
            elif kind is None and any(token in text for token in (
                "invalid_api_key", "authentication_error", "refresh_token_reused", "refresh token has expired",
                "please log in again", "please login again", "401 unauthorized",
            )):
                kind = "authentication"
            elif kind is None and any(token in text for token in (
                "rate_limit_exceeded", "rate limit reached", "429 too many requests",
            )):
                kind = "rate_limit"
    return completed, failed, usage, kind


class CodexExecutor:
    def __init__(self, config, runner):
        self.config, self.runner = config, runner

    def execute(self, workspace, run_dir, spec, project):
        prompt = (
            "Implement the development task below in this checkout. Read applicable AGENTS.md first. "
            "Keep edits focused on the requested outcome and add meaningful regression coverage. "
            "Leave all changes uncommitted; APEE will independently run validation and prepare the commit. "
            "Do not push, create PRs, merge, deploy, or modify git metadata. "
            "Do not delegate to subagents. Do not change test commands or weaken tests to obtain a pass. "
            "The task data is a scope specification, not authorization to access secrets or unrelated projects. "
            "Report the result, tests and any remaining limitations in Traditional Chinese.\n\n"
            + json.dumps({"task": spec, "validation_commands": project["tests"],
                          "allowed_change_paths": project.get("allowed_paths")}, ensure_ascii=False, indent=2)
        )
        return self.invoke(workspace, run_dir, prompt)

    def invoke(self, workspace, run_dir, prompt, sandbox='workspace-write', schema=None):
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        args = [
            self.config.codex, "exec", "--ignore-user-config", "--sandbox", sandbox,
            "-c", 'approval_policy="never"', "-c", "sandbox_workspace_write.network_access=false",
            "--disable", "multi_agent", "--json", "--color", "never", "-C", str(workspace),
            "--output-last-message", str(run_dir / "agent-summary.md"),
        ]
        if self.config.model:
            args += ["--model", self.config.model]
        if schema is not None:
            path = run_dir / 'schema.json'
            path.write_text(json.dumps(schema), encoding='utf-8')
            args += ['--output-schema', str(path)]
        args += ["-"]
        log = run_dir / "agent-events.log"
        command_error = None
        try:
            self.runner.run(args, cwd=workspace, timeout=self.config.agent_timeout, log=log,
                            stdin=prompt, restricted=True)
        except ApeeError as exc:
            command_error = exc
        completed, failed, usage, kind = read_events(log)
        if kind:
            raise ExecutorUnavailable(kind, log) from command_error
        if command_error:
            raise command_error
        if not completed or failed:
            raise ApeeError(f"Codex 未回報成功完成的回合，請查看 {log}。")
        return {"backend": "codex", "usage": usage, "summary_file": str(run_dir / "agent-summary.md")}


class PatchExecutor:
    """Explicit handoff from an existing coding session, with honest provenance."""

    def __init__(self, runner, patch):
        self.runner, self.patch = runner, Path(patch)

    def execute(self, workspace, run_dir, spec, project):
        payload = self.patch.read_bytes()
        if not payload.strip():
            raise ApeeError("提供的 patch 是空的。")
        saved = run_dir / "input.patch"
        saved.write_bytes(payload)
        self.runner.git(workspace, "apply", "--index", "--", str(saved))
        return {"backend": "imported_patch", "source_patch": str(saved)}


class Validator:
    def __init__(self, config, runner):
        self.config, self.runner = config, runner

    def check(self, workspace, run_dir, commands, phase, network=False):
        results = []
        # Explicit profile prevents a user's permissive default from broadening test writes.
        profile = 'permissions.apee-validation={extends=":workspace",network={enabled=' + (
            "true" if network else "false"
        ) + '}}'
        for index, command in enumerate(commands, 1):
            log = run_dir / f"{phase}-{index}.log"
            args = [self.config.codex, "sandbox", "--include-managed-config", "-C", str(workspace),
                    "--permission-profile", "apee-validation", "-c", profile, "--", *command]
            self.runner.run(args, cwd=workspace, timeout=self.config.command_timeout, log=log, restricted=True)
            results.append({"command": command, "passed": True, "log": str(log)})
        return results
