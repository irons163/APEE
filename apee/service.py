"""User launchd agents that run one bounded APEE cycle per scheduled wake."""

import fcntl
import hashlib
import json
import os
import plistlib
import shutil
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import ApeeError, BusyError, Config, positive_int
from .process import Runner, limited_env


ENV_KEYS = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL",
            "CODEX_HOME", "GH_CONFIG_DIR", "XDG_CONFIG_HOME"}
FIXED_ENV = dict(PYTHONDONTWRITEBYTECODE="1", GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1",
                 GH_HOST="github.com", CI="1")

PROBE = """
import json, os, sys, tempfile
from pathlib import Path
stage = "source"
try:
    sys.path.insert(0, sys.argv[1])
    import apee
    from apee.cli import main
    from apee.config import Config
    if Path(apee.__file__).resolve().parent != Path(sys.argv[1]) / "apee":
        raise RuntimeError("different runtime")
    stage = "working_directory"
    os.chdir(sys.argv[1])
    os.getcwd()
    stage = "config"
    config = Config(sys.argv[2])
    if str(config.state_dir) != sys.argv[3]:
        raise RuntimeError("state directory changed")
    stage = "state_directory"
    logs = config.state_dir / "service"
    logs.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=logs) as output:
        output.write(b"apee background access check")
except Exception as exc:
    print(json.dumps(dict(ok=False, stage=stage, error_type=type(exc).__name__)))
    raise SystemExit(1)
print(json.dumps(dict(ok=True)))
"""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".apee-", delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Service:
    def __init__(self, config_path, runner=None, agents_dir=None, registry_dir=None):
        self.config_path = Path(config_path).expanduser().resolve()
        self.runner = runner or Runner()
        self.label = "com.apee.worker." + digest(str(self.config_path).encode())[:12]
        self.domain = f"gui/{os.getuid()}"
        self.target = self.domain + "/" + self.label
        self.agents_dir = Path(agents_dir) if agents_dir is not None else Path.home() / "Library/LaunchAgents"
        self.registry_dir = (Path(registry_dir) if registry_dir is not None else
                             Path.home() / "Library/Application Support/APEE/services")
        self.plist_path = self.agents_dir / (self.label + ".plist")
        self.record_path = self.registry_dir / (self.label + ".json")
        self.source_root = Path(__file__).resolve().parents[1]
        self.python = str(Path(sys.executable).resolve())

    def environment(self):
        env = {key: value for key, value in limited_env().items() if key in ENV_KEYS}
        for key in ("GH_CONFIG_DIR", "XDG_CONFIG_HOME"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        paths = [str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
        codex = Path(Config(self.config_path).codex)
        if codex.is_absolute():
            paths.insert(0, str(codex.parent))
        paths += [part for part in os.environ.get("PATH", "").split(os.pathsep) if Path(part).is_absolute()]
        paths += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        env["PATH"] = os.pathsep.join(dict.fromkeys(paths))
        env.update(FIXED_ENV)
        return env

    def definition(self, interval=300, max_tasks=1, local_only=False, environment=None):
        positive_int(interval, "service interval")
        if interval < 60:
            raise ApeeError("服務間隔至少為 60 秒。")
        positive_int(max_tasks, "service max_tasks", 1000)
        if type(local_only) is not bool:
            raise ApeeError("local_only 必須是布林值。")
        env = dict(environment) if environment is not None else self.environment()
        if (set(env) - ENV_KEYS - set(FIXED_ENV) or
                any(not isinstance(value, str) or not value or "\x00" in value for value in env.values()) or
                any(env.get(key) != value for key, value in FIXED_ENV.items()) or
                not env.get("PATH") or any(not Path(part).is_absolute() for part in env["PATH"].split(os.pathsep))):
            raise ApeeError("背景環境設定無法核對；請重新安裝服務。")
        for key in ("HOME", "CODEX_HOME", "GH_CONFIG_DIR", "XDG_CONFIG_HOME"):
            if key in env and not Path(env[key]).is_absolute():
                raise ApeeError(f"背景環境的 {key} 必須是絕對路徑。")
        config = Config(self.config_path)
        if Path(config.codex).is_absolute() and str(Path(config.codex).parent) not in env['PATH'].split(os.pathsep):
            raise ApeeError('Codex CLI 位置已變更；請移除並重新安裝背景服務以更新工具環境。')
        args = ["/usr/bin/env", "-i", *[f"{key}={value}" for key, value in sorted(env.items())],
                self.python, "-B", "-u", "-m", "apee", "--config", str(self.config_path),
                "work", "--max-cycles", "1", "--max-tasks", str(max_tasks),
                "--expected-state-dir", str(config.state_dir)]
        if local_only:
            args.append("--local-only")
        return dict(Label=self.label, ProgramArguments=args, WorkingDirectory=str(self.source_root),
                    EnvironmentVariables=env, StartInterval=interval, RunAtLoad=False, KeepAlive=False,
                    ProcessType="Background", ExitTimeOut=30, Umask=0o077,
                    StandardOutPath=str(config.state_dir / "service/stdout.log"),
                    StandardErrorPath=str(config.state_dir / "service/stderr.log"))

    def render(self, output=None, **options):
        definition = self.definition(**options)
        payload = plistlib.dumps(definition, sort_keys=True)
        if output is not None:
            output = Path(output).expanduser().absolute()
            if output.is_symlink() or (output.exists() and output.read_bytes() != payload):
                raise ApeeError("輸出位置已有不同內容，未覆寫。")
            if not output.exists():
                atomic_write(output, payload)
        return dict(label=self.label, target=self.target, plist_path=str(self.plist_path),
                    output=str(output) if output is not None else None, definition=definition)

    def _launchctl(self, *args):
        if sys.platform != "darwin":
            raise ApeeError("服務管理目前只支援 macOS launchd。")
        return self.runner.run(["/bin/launchctl", *args], timeout=40)

    def _job(self, label=None):
        label = label or self.label
        if (self._launchctl("managername").strip() != "Aqua" or
                self._launchctl("manageruid").strip() != str(os.getuid())):
            raise ApeeError("請在目前使用者的 macOS 桌面登入工作階段管理服務。")
        # launchctl print explicitly has no stable output API. list's documented
        # PID/status/label columns provide the small amount of state needed here.
        lines = self._launchctl("list").splitlines()
        if not lines or lines[0].split() != ["PID", "Status", "Label"]:
            raise ApeeError("無法辨識 launchctl 狀態，未假定服務已停止。")
        found = []
        for line in lines[1:]:
            if not line.strip():
                continue
            fields = line.split(None, 2)
            try:
                if len(fields) != 3:
                    raise ValueError()
                pid = None if fields[0] == "-" else int(fields[0])
                code = int(fields[1])
            except ValueError as exc:
                raise ApeeError("無法辨識 launchctl 狀態，未假定服務已停止。") from exc
            if fields[2] == label:
                found.append(dict(loaded=True, running=pid is not None, pid=pid, last_exit_status=code))
        if len(found) > 1:
            raise ApeeError("服務名稱重複，狀態無法核對。")
        return found[0] if found else dict(loaded=False, running=False, pid=None, last_exit_status=None)

    @contextmanager
    def _lock(self):
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        with (self.registry_dir / "management.lock").open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BusyError("另一個服務管理操作正在進行。") from exc
            yield

    def _record(self):
        if self.record_path.is_symlink() or self.plist_path.is_symlink():
            raise ApeeError("服務檔或安裝紀錄為符號連結，未操作。")
        if not self.record_path.exists():
            return None
        try:
            record = json.loads(self.record_path.read_text(encoding="utf-8"))
            expected = dict(version=1, label=self.label, config=str(self.config_path), plist_path=str(self.plist_path))
            if any(record.get(key) != value for key, value in expected.items()):
                raise ValueError()
            options, definition = record["options"], record["definition"]
            if (set(options) != {"interval", "max_tasks", "local_only"} or
                    type(options["interval"]) is not int or not 60 <= options["interval"] <= 86400 or
                    type(options["max_tasks"]) is not int or not 1 <= options["max_tasks"] <= 1000 or
                    type(options["local_only"]) is not bool or definition["Label"] != self.label or
                    definition["StartInterval"] != options["interval"] or
                    any(not isinstance(definition[key], str) for key in
                        ("WorkingDirectory", "StandardOutPath", "StandardErrorPath")) or
                    not isinstance(definition["EnvironmentVariables"], dict) or
                    not isinstance(definition["ProgramArguments"], list) or
                    not Path(record["state_dir"]).is_absolute()):
                raise ValueError()
            payload = plistlib.dumps(record["definition"], sort_keys=True)
            if digest(payload) != record["sha256"]:
                raise ValueError()
            if self.plist_path.exists():
                info = self.plist_path.stat()
                if (info.st_uid != os.getuid() or info.st_mode & 0o022 or
                        digest(self.plist_path.read_bytes()) != record["sha256"]):
                    raise ValueError()
            return record
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            raise ApeeError("服務檔與安裝紀錄無法核對，未覆寫或移除。") from exc

    def _current(self, record):
        return self.definition(**record["options"], environment=record["definition"]["EnvironmentVariables"])

    def status(self):
        record, job = self._record(), self._job()
        current = False
        if record:
            try:
                current = self._current(record) == record["definition"]
            except ApeeError:
                pass
        return dict(label=self.label, target=self.target, config=str(self.config_path),
                    installed=self.plist_path.exists(), owned=record is not None,
                    needs_reinstall=bool(record and not current), plist_path=str(self.plist_path),
                    options=record["options"] if record else None,
                    stdout=record["definition"]["StandardOutPath"] if record else None,
                    stderr=record["definition"]["StandardErrorPath"] if record else None, **job)

    def _preflight(self, definition):
        config = Config(self.config_path)
        env = definition["EnvironmentVariables"]
        if "/" in config.codex and not Path(config.codex).is_absolute():
            raise ApeeError("背景服務的 codex 必須是執行檔名稱或絕對路徑。")
        tools = {name: shutil.which(command, path=env["PATH"]) for name, command in
                 (("Git", "git"), ("GitHub CLI", "gh"), ("Codex CLI", config.codex))}
        if not all(tools.values()):
            raise ApeeError("背景 PATH 找不到 Git、GitHub CLI 或 Codex CLI；請先檢查 doctor 與服務預覽。")
        prefix = ["/usr/bin/env", "-i", *[f"{key}={value}" for key, value in sorted(env.items())]]
        checks = [("Git", [tools["Git"], "--version"]),
                  ("GitHub CLI", [tools["GitHub CLI"], "--version"]),
                  ("GitHub 登入", [tools["GitHub CLI"], "auth", "status"]),
                  ("Codex CLI", [tools["Codex CLI"], "--version"]),
                  ("Codex 登入", [tools["Codex CLI"], "login", "status"]),
                  ("Codex sandbox 介面", [tools["Codex CLI"], "sandbox", "--help"]),
                  ("Codex exec 介面", [tools["Codex CLI"], "exec", "--help"])]
        for name, command in checks:
            try:
                output = self.runner.run(prefix + command, cwd=self.source_root, timeout=30)
            except ApeeError:
                raise ApeeError(f"背景環境的 {name} 不可用；請完成安裝或登入後再啟動。") from None
            required = {"Codex sandbox 介面": ('--permission-profile', '--include-managed-config'),
                        "Codex exec 介面": ('--ignore-user-config', '--output-schema', '--output-last-message')}.get(name, ())
            if any(flag not in output for flag in required):
                raise ApeeError(f"背景環境的 {name} 缺少必要支援。")
        self._background_probe(definition, prefix, config.state_dir)

    def _background_probe(self, definition, prefix, state_dir):
        # Foreground access does not prove launchd can read Documents/Desktop.
        # This one-shot job imports the runtime and checks storage, never work().
        root = Path(tempfile.mkdtemp(prefix="access-check-", dir=self.registry_dir))
        label = self.label + ".preflight." + uuid.uuid4().hex[:12]
        target = self.domain + "/" + label
        output = root / "result.json"
        probe = dict(Label=label, ProgramArguments=prefix + [self.python, "-B", "-u", "-c", PROBE,
                     str(self.source_root), str(self.config_path), str(state_dir)],
                     WorkingDirectory="/", RunAtLoad=False, KeepAlive=False, ProcessType="Background",
                     ExitTimeOut=5,
                     StandardOutPath=str(output), StandardErrorPath=str(root / "stderr.log"), Umask=0o077)
        path = root / "probe.plist"
        try:
            atomic_write(path, plistlib.dumps(probe, sort_keys=True))
            self._launchctl("bootstrap", self.domain, str(path))
            self._launchctl("kickstart", target)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    result = json.loads(output.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    time.sleep(0.05)
                    continue
                if isinstance(result, dict) and result.get("ok") is True:
                    return
                if (isinstance(result, dict) and result.get("ok") is False and
                        result.get("stage") in ("source", "working_directory", "config", "state_directory")):
                    locations = dict(source=self.source_root, working_directory=self.source_root,
                                     config=self.config_path, state_directory=state_dir)
                    raise ApeeError(f"launchd 無法存取 {locations[result['stage']]}。請將程式、設定及狀態資料放在背景可存取的目錄，或在 macOS 隱私權設定允許存取後重試。")
                raise ApeeError("背景存取檢查回傳無法辨識的結果，未安裝或啟動工作排程。")
            raise ApeeError("背景存取檢查未完成，未安裝或啟動工作排程。")
        finally:
            try:
                if self._job(label)["loaded"]:
                    self._launchctl("bootout", target)
                if self._job(label)["loaded"]:
                    raise ApeeError("存取檢查程序尚未停止。")
            except ApeeError as exc:
                raise ApeeError(f"背景存取檢查無法完成清理，檔案保留於 {root}；請核對 {target}。") from exc
            shutil.rmtree(root)

    def install(self, interval=300, max_tasks=1, local_only=False):
        with self._lock():
            definition = self.definition(interval, max_tasks, local_only)
            payload = plistlib.dumps(definition, sort_keys=True)
            record, job = self._record(), self._job()
            if record:
                if record["definition"] != definition:
                    raise ApeeError("服務已有不同設定；請先 stop、uninstall，再重新 install。")
            elif self.plist_path.exists() or job["loaded"]:
                raise ApeeError("此名稱已有未核對的服務或檔案，未覆寫或接管。")
            config = Config(self.config_path)
            for path in self.registry_dir.glob("com.apee.worker.*.json"):
                if path == self.record_path:
                    continue
                try:
                    other = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(other, dict):
                        raise ValueError()
                    if other.get("state_dir") == str(config.state_dir):
                        raise ApeeError("同一份狀態資料已有服務設定；請先處理原服務。")
                except (OSError, ValueError):
                    raise ApeeError("服務登錄資料無法完整核對，未新增排程。") from None
            self._preflight(definition)
            Path(definition["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)
            if record is None:
                record = dict(version=1, label=self.label, config=str(self.config_path),
                              state_dir=str(config.state_dir), plist_path=str(self.plist_path),
                              options=dict(interval=interval, max_tasks=max_tasks, local_only=local_only),
                              sha256=digest(payload), definition=definition)
                atomic_write(self.record_path, (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode())
            if not self.plist_path.exists():
                atomic_write(self.plist_path, payload)
            # Ownership is saved before registration. Failed/ambiguous registration
            # leaves a managed definition that status/start/uninstall can reconcile.
            self._launchctl("enable", self.target)
            if not job["loaded"]:
                self._launchctl("bootstrap", self.domain, str(self.plist_path))
            result = self.status()
            if not result["loaded"]:
                raise ApeeError("服務登錄結果尚未核對；檔案與紀錄已保留，請用 service status 檢查。")
            return result

    def start(self):
        with self._lock():
            record = self._record()
            if not record or not self.plist_path.exists():
                raise ApeeError("尚未安裝完整服務，請先使用 service install。")
            if self._current(record) != record["definition"]:
                raise ApeeError("服務的程式、設定或狀態目錄已變更；請停止並重新安裝。")
            job = self._job()
            if job["running"]:
                return self.status()
            self._preflight(record["definition"])
            self._launchctl("enable", self.target)
            if not job["loaded"]:
                self._launchctl("bootstrap", self.domain, str(self.plist_path))
            self._launchctl("kickstart", self.target)
            return self.status()

    def _stop(self):
        job = self._job()
        self._launchctl("disable", self.target)
        if job["loaded"]:
            self._launchctl("bootout", self.target)
        deadline = time.monotonic() + 5
        while self._job()["loaded"]:
            if time.monotonic() >= deadline:
                raise ApeeError("服務尚未停止，檔案與紀錄已保留。")
            time.sleep(0.1)

    def stop(self):
        with self._lock():
            if not self._record():
                if self.plist_path.exists() or self._job()["loaded"]:
                    raise ApeeError("缺少可核對的安裝紀錄，未停止未知服務。")
                return self.status()
            self._stop()
            return self.status()

    def uninstall(self):
        with self._lock():
            if not self._record():
                if self.plist_path.exists() or self._job()["loaded"]:
                    raise ApeeError("缺少可核對的安裝紀錄，未移除未知服務。")
                return self.status()
            self._stop()
            self.plist_path.unlink(missing_ok=True)
            self.record_path.unlink()
            return self.status()
