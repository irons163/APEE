"""Argument-array subprocesses with logs, deadlines and process-group cleanup."""

import os
import signal
import subprocess
from contextlib import contextmanager
from pathlib import Path

from .config import ApeeError
from .leases import attempt_lease


def limited_env():
    allowed = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "CODEX_HOME")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(PYTHONDONTWRITEBYTECODE="1", GIT_TERMINAL_PROMPT="0", CI="1")
    return env


class Runner:
    @contextmanager
    def lease(self, run_dir):
        run_dir = Path(run_dir).resolve()
        previous = getattr(self, "_lease", None)
        if previous:
            if previous[0] != run_dir:
                raise ApeeError("不能在同一執行器中同時持有不同嘗試的執行鎖。")
            yield
            return
        with attempt_lease(run_dir) as descriptor:
            self._lease = (run_dir, descriptor)
            try:
                yield
            finally:
                self._lease = previous

    def run(self, args, cwd=None, timeout=120, log=None, stdin=None, restricted=False):
        env = limited_env() if restricted else dict(os.environ)
        env.update(GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1", GH_HOST="github.com")
        # A caller's GH_REPO must not redirect a centrally specified repository.
        env.pop("GH_REPO", None)
        handle = None
        if log:
            log = Path(log)
            log.parent.mkdir(parents=True, exist_ok=True)
            handle = log.open("w", encoding="utf-8")
        try:
            lease = getattr(self, "_lease", None)
            process = subprocess.Popen(
                [str(arg) for arg in args], cwd=cwd, env=env, text=True,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=handle or subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(lease[1],) if lease else (),
            )
            try:
                output, _ = process.communicate(stdin, timeout=timeout)
            except BaseException:
                self.stop(process)
                raise
            self.stop(process)
            if process.returncode:
                detail = f"記錄：{log}" if log else (output or "")[-2000:].strip()
                raise ApeeError(f"{args[0]} 結束碼 {process.returncode}。{detail}")
            return output or ""
        except subprocess.TimeoutExpired as exc:
            raise ApeeError(f"命令超過 {timeout} 秒，已停止程序群組。記錄：{log}") from exc
        except OSError as exc:
            raise ApeeError(f"無法執行 {args[0]}: {exc}") from exc
        finally:
            if handle:
                handle.close()

    @staticmethod
    def stop(process):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        # Kill descendants even if the group leader exited promptly.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()

    def git(self, workspace, *args, **kwargs):
        return self.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
            cwd=workspace, **kwargs,
        ).strip()
