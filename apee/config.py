"""Explicit, centrally managed execution settings; never loaded from target repos."""

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath


class ApeeError(Exception):
    pass


class BusyError(ApeeError):
    """Another process currently owns the requested engine lock."""


class DispatchBlocked(ApeeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason["message"])


def repo_name(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", value
    ):
        raise ApeeError("repository 必須是 owner/repo。")
    return value.lower()


def positive_int(value, name, maximum=86400):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ApeeError(f"{name} 必須是 1–{maximum} 的整數。")
    return value


def commands(value, name, required=False):
    if not isinstance(value, list) or (required and not value):
        raise ApeeError(f"{name} 必須包含明確的命令陣列。")
    for command in value:
        if not isinstance(command, list) or not command or any(
            not isinstance(part, str) or not part or "\x00" in part for part in command
        ):
            raise ApeeError(f"{name} 的每項命令必須是非空字串陣列。")
    return value


def money(value, name, positive=False):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,9}(?:\.\d{1,9})?", value):
        raise ApeeError(f"{name} 必須是非負十進位字串。")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ApeeError(f"{name} 無法辨識。") from exc
    if positive and result <= 0:
        raise ApeeError(f"{name} 必須大於零。")
    return result


class Config:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ApeeError(f"無法讀取設定 {self.path}: {exc}") from exc
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            raise ApeeError("設定 version 必須是 1。")
        self.data = data
        state = data.get("state_dir", ".apee")
        if not isinstance(state, str) or not state:
            raise ApeeError("state_dir 必須是路徑字串。")
        self.state_dir = (self.path.parent / Path(state).expanduser()).resolve()
        self.codex = data.get("codex", "codex")
        self.model = data.get("model")
        if not isinstance(self.codex, str) or not self.codex:
            raise ApeeError("codex 必須是執行檔名稱或絕對路徑。")
        if self.model is not None and (not isinstance(self.model, str) or not self.model):
            raise ApeeError("model 必須是非空字串，或省略以使用 CLI 預設模型。")
        self.agent_timeout = positive_int(data.get("agent_timeout_seconds", 900), "agent_timeout_seconds")
        self.command_timeout = positive_int(data.get("command_timeout_seconds", 300), "command_timeout_seconds")
        self.max_tasks = positive_int(data.get("max_tasks_per_cycle", 1), "max_tasks_per_cycle", 20)
        self.daily_attempt_limit = positive_int(data.get("daily_attempt_limit", 10), "daily_attempt_limit", 1000)
        self.max_pr_checks = positive_int(data.get("max_pr_checks_per_cycle", 20), "max_pr_checks_per_cycle", 100)
        recovery = data.get("auto_recovery", {})
        if (not isinstance(recovery, dict) or set(recovery) - {"enabled", "max_retries", "cooldown_seconds", "max_per_cycle"}
                or type(recovery.get("enabled", False)) is not bool):
            raise ApeeError("auto_recovery 設定無法辨識。")
        retries = recovery.get("max_retries", 1)
        if type(retries) is not int or not 0 <= retries <= 5:
            raise ApeeError("auto_recovery.max_retries 必須是 0–5 的整數。")
        cooldown = positive_int(recovery.get("cooldown_seconds", 300), "auto_recovery.cooldown_seconds")
        if cooldown < 60:
            raise ApeeError("自動恢復觀察時間至少 60 秒。")
        self.auto_recovery = dict(enabled=recovery.get("enabled", False), max_retries=retries,
                                 cooldown_seconds=cooldown,
                                 max_per_cycle=positive_int(recovery.get("max_per_cycle", 1), "auto_recovery.max_per_cycle", 20))
        budget = data.get("budget", {})
        if not isinstance(budget, dict) or set(budget) - {"daily_tokens", "daily_estimated_usd", "prices_per_million"}:
            raise ApeeError("budget 設定無法辨識。")
        if "daily_tokens" in budget:
            positive_int(budget["daily_tokens"], "budget.daily_tokens", 10**12)
        if "daily_estimated_usd" in budget:
            money(budget["daily_estimated_usd"], "budget.daily_estimated_usd", positive=True)
            if not self.model or "prices_per_million" not in budget:
                raise ApeeError("費用預算需要明確的 model 與 prices_per_million；不推測實際帳單。")
        if "prices_per_million" in budget:
            prices = budget["prices_per_million"]
            if not isinstance(prices, dict) or set(prices) != {"input", "cached_input", "output"} or not self.model:
                raise ApeeError("單價需要明確 model，以及 input、cached_input、output 三個欄位。")
            for key, value in prices.items():
                money(value, "budget.prices_per_million." + key)
        self.budget = budget
        quality = data.get('quality', {})
        if (not isinstance(quality, dict) or set(quality) - {'enabled', 'max_repairs'}
                or type(quality.get('enabled', False)) is not bool):
            raise ApeeError('quality 設定無法辨識。')
        repairs = quality.get('max_repairs', 1)
        if type(repairs) is not int or not 0 <= repairs <= 3:
            raise ApeeError('quality.max_repairs 必須是 0–3 的整數。')
        self.quality = dict(enabled=quality.get('enabled', False), max_repairs=repairs)
        projects = data.get("projects", [])
        if not isinstance(projects, list):
            raise ApeeError("projects 必須是陣列。")
        self.projects = {}
        for item in projects:
            if not isinstance(item, dict):
                raise ApeeError("每個 project 必須是物件。")
            name = repo_name(item.get("repo"))
            if name in self.projects:
                raise ApeeError(f"重複的 repository: {name}")
            enabled = item.get("enabled", False)
            publish = item.get("publish_draft", False)
            setup_network = item.get("setup_network", False)
            if any(type(flag) is not bool for flag in (enabled, publish, setup_network)):
                raise ApeeError("enabled、publish_draft 和 setup_network 必須是布林值。")
            label = item.get("issue_label", "apee:ready")
            if not isinstance(label, str) or not label.strip() or "," in label:
                raise ApeeError("issue_label 必須是一個非空標籤。")
            tests = commands(item.get("tests", []), f"{name}.tests", required=enabled)
            setup = commands(item.get("setup", []), f"{name}.setup")
            config_dir = str(self.path.parent)
            tests = [[part.replace("{config_dir}", config_dir) for part in command]
                     for command in tests]
            setup = [[part.replace("{config_dir}", config_dir) for part in command]
                     for command in setup]
            allowed = item.get("allowed_paths")
            if allowed is not None:
                if not isinstance(allowed, list) or not allowed:
                    raise ApeeError("allowed_paths 必須是非空的相對檔案路徑陣列，或省略。")
                for entry in allowed:
                    if not isinstance(entry, str) or not entry:
                        raise ApeeError("allowed_paths 的項目必須是非空字串。")
                    path = PurePosixPath(entry)
                    if (path.is_absolute() or not path.parts or ".." in path.parts
                            or path.as_posix() != entry or path.parts[0] in (".git", ".codex")):
                        raise ApeeError(f"不允許的工作範圍路徑：{entry}")
            self.projects[name] = dict(
                item, repo=name, enabled=enabled, publish_draft=publish,
                issue_label=label, tests=tests, setup=setup, allowed_paths=allowed,
                priority=positive_int(item.get("priority", 3), "priority", 5),
            )

    def project(self, name):
        name = repo_name(name)
        project = self.projects.get(name)
        if not project or not project["enabled"]:
            raise ApeeError(f"{name} 尚未在設定中啟用。")
        return project
