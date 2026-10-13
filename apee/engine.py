"""One-task-at-a-time GitHub development, validation and review handoff."""

import hashlib
import json
import uuid
from pathlib import Path

from .config import ApeeError, Config, DispatchBlocked
from .executors import CodexExecutor, ExecutorUnavailable, PatchExecutor, Validator
from .github import GitHub
from .leases import attempt_lease, lease_state
from .process import Runner
from .reviews import snapshot, summary
from .store import Store, now


RECOVERABLE_STATES = {"preparing", "running", "testing"}


def issue_spec(issue):
    return dict(source="github", number=issue["number"], title=issue["title"],
                body=issue.get("body") or "", url=issue["html_url"], updated_at=issue["updated_at"])


def eligible(issue, project):
    return (issue.get("state") == "open" and "pull_request" not in issue
            and project["issue_label"] in [label["name"] for label in issue.get("labels", [])])


class Engine:
    def __init__(self, config, runner=None, github=None, executor=None, validator=None):
        self.config = config
        self.runner = runner or Runner()
        self.github = github or GitHub(self.runner)
        self.executor = executor or CodexExecutor(config, self.runner)
        self.validator = validator or Validator(config, self.runner)
        self.store = Store(config.state_dir)

    def reload_config(self):
        config = Config(self.config.path)
        if config.state_dir != self.config.state_dir:
            raise ApeeError("連續執行期間不能更換 state_dir；請停止後重新啟動。")
        self.config = config
        if isinstance(self.executor, CodexExecutor):
            self.executor.config = config
        if isinstance(self.validator, Validator):
            self.validator.config = config

    def dispatch_blocker(self, use_codex=True):
        paused = self.store.control("paused")
        if paused:
            return {"kind": "paused", "message": "已暫停開始新任務。", "detail": paused}
        pending = [item for item in self.recovery_status()
                   if item["lease"] in ("active", "unreadable")
                   or (item["current_attempt"] and item["task_state"] in RECOVERABLE_STATES)]
        if pending:
            return {"kind": "recovery_required", "attempts": pending,
                    "message": "尚有未完成嘗試或殘留程序；執行 recover 查看，處理後才能開始新任務。"}
        hold = self.store.control("codex_hold") if use_codex else None
        if hold:
            return hold
        attempts = self.store.attempts_today()
        if attempts["used"] >= self.config.daily_attempt_limit:
            return dict(attempts, kind="daily_limit", limit=self.config.daily_attempt_limit,
                        message="已達每日嘗試上限；下個 UTC 日期才會恢復派工。")
        return None

    def recovery_status(self):
        tasks = {task["id"]: task for task in self.store.tasks()}
        result, recorded = [], set()
        for run in self.store.runs():
            task = tasks[run["task_id"]]
            current = task["run_id"] == run["id"]
            state = lease_state(self.config.state_dir / "runs" / run["id"])
            if not (state in ("active", "unreadable") or
                    (current and task["state"] in RECOVERABLE_STATES | {"publishing"})):
                continue
            if state == "active":
                action = "wait_for_processes"
            elif state == "unreadable":
                action = "inspect_lease"
            elif task["state"] == "publishing":
                action = "publish"
            else:
                action = "recover" if state == "idle" else (
                    "inspect_lease" if run.get("lease_version") else "inspect_legacy"
                )
            result.append(dict(task=task["id"], task_state=task["state"], run_id=run["id"],
                               current_attempt=current, lease=state, action=action,
                               workspace=run["workspace"]))
            recorded.add(task["id"])
        for task in tasks.values():
            if task["state"] in RECOVERABLE_STATES and task["id"] not in recorded:
                result.append(dict(task=task["id"], task_state=task["state"], run_id=task["run_id"],
                                   current_attempt=True, lease="missing", action="inspect_legacy",
                                   workspace=None))
        return result

    def recover(self, task_id, legacy=False):
        task = self.store.task(task_id)
        run = self.store.run(task_id)
        if task["state"] == "publishing":
            return dict(task=task_id, state="publishing", run_id=run["id"], next_action="publish",
                        message="交付結果需核對；保留原狀態，使用 publish 核對既有 PR。")
        if task["state"] == "interrupted" and run.get("recovery"):
            return dict(task=task_id, state="interrupted", run_id=run["id"],
                        workspace=run["workspace"], next_action="retry")
        if task["state"] not in RECOVERABLE_STATES:
            raise ApeeError("只有未完成的準備、開發或測試嘗試可以恢復。")
        run_dir = self.config.state_dir / "runs" / run["id"]

        def mark_interrupted(proof):
            run["recovery"] = dict(at=now(), previous_state=task["state"], proof=proof)
            run.setdefault("error", "執行程序已停止；工作副本已保留，可明確 retry 後使用新的 checkout。")
            self.store.transition(task_id, "interrupted", {
                "reason": "recovered_abandoned_attempt", "previous_state": task["state"], "proof": proof,
            }, run=run)

        try:
            with attempt_lease(run_dir, create=False):
                mark_interrupted("lease_idle")
        except FileNotFoundError as exc:
            if not legacy or run.get("lease_version") is not None:
                raise ApeeError("缺少執行鎖紀錄，狀態保持不變；僅舊版嘗試可在人工確認程序結束後加 --legacy。") from exc
            mark_interrupted("legacy_manually_checked")
        return dict(task=task_id, state="interrupted", run_id=run["id"],
                    workspace=run["workspace"], next_action="retry")

    def retry(self, task_id):
        task = self.store.task(task_id)
        if task["state"] not in ("failed", "interrupted", "waiting"):
            raise ApeeError(f"此操作不適用於 {task['state']} 狀態。")
        for run in self.store.runs(task_id):
            if lease_state(self.config.state_dir / "runs" / run["id"]) in ("active", "unreadable"):
                raise ApeeError("這項任務的舊嘗試仍有執行程序，或執行鎖無法讀取；未重新入列。")
        self.store.transition(task_id, "queued", {"requested_by": "user"})

    def sync(self):
        added, errors = [], []
        for project in sorted(self.config.projects.values(), key=lambda p: p["priority"]):
            if not project["enabled"]:
                continue
            try:
                for issue in self.github.issues(project):
                    if not eligible(issue, project):
                        continue
                    task_id = f"{project['repo']}#{issue['number']}"
                    if self.store.enqueue(task_id, project["repo"], issue_spec(issue), project["priority"]):
                        added.append(task_id)
            except ApeeError as exc:
                errors.append({"repo": project["repo"], "error": str(exc)})
        return {"added": added, "errors": errors}

    def enqueue(self, repo, title, body):
        project = self.config.project(repo)
        if not title.strip() or not body.strip():
            raise ApeeError("任務需要標題，以及包含完成條件的描述。")
        spec = dict(source="manual", title=title.strip(), body=body.strip())
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]
        task_id = f"{project['repo']}@{digest}"
        self.store.enqueue(task_id, project["repo"], spec, project["priority"])
        return task_id

    def next_task(self):
        candidates = [task for task in self.store.tasks() if task["state"] == "queued"
                      and self.config.projects.get(task["repo"], {}).get("enabled")]
        candidates.sort(key=lambda t: (self.config.projects[t["repo"]]["priority"], t["created"], t["id"]))
        return candidates[0]["id"] if candidates else None

    def execute(self, task_id, local_only=False, patch=None):
        task = self.store.task(task_id)
        project = self.config.project(task["repo"])
        if task["state"] != "queued":
            raise ApeeError(f"任務狀態是 {task['state']}，不會重複執行。")
        blocker = self.dispatch_blocker(use_codex=patch is None)
        if blocker:
            raise DispatchBlocked(blocker)
        run_id = uuid.uuid4().hex
        run_dir = self.config.state_dir / "runs" / run_id
        run_dir.mkdir(parents=True)
        workspace = run_dir / "repo"
        spec = task["spec"]
        run = dict(id=run_id, task_id=task_id, repo=task["repo"], started_at=now(),
                   workspace=str(workspace), run_dir=str(run_dir), project=project, spec=spec,
                   branch=f"apee/task-{run_id[:16]}", lease_version=1)
        with self.runner.lease(run_dir):
            return self._execute(task, project, run, local_only, patch)

    def _execute(self, task, project, run, local_only, patch):
        task_id = task["id"]
        run_dir, workspace = Path(run["run_dir"]), Path(run["workspace"])
        spec = run["spec"]
        self.store.transition(task_id, "preparing", run=run)
        try:
            if spec["source"] == "github":
                issue = self.github.issue(task["repo"], spec["number"])
                if not eligible(issue, project):
                    raise ApeeError("Issue 已關閉或已移除派工標籤。")
                # Freeze the latest source before executing; the queued snapshot remains in history.
                spec = run["spec"] = issue_spec(issue)
            meta = self.github.metadata(task["repo"])
            run["base"] = meta["default_branch"]
            self.github.clone(task["repo"], workspace, run["base"])
            run["base_sha"] = self.runner.git(workspace, "rev-parse", "HEAD")
            self.runner.git(workspace, "checkout", "-b", run["branch"])
            self.validator.check(workspace, run_dir, project["setup"], "setup", project.get("setup_network", False))
            if self.runner.git(workspace, "status", "--porcelain"):
                raise ApeeError("依賴準備改動了版本控制內容；請先修正 setup 設定。")
            self.store.transition(task_id, "running", run=run)
            executor = PatchExecutor(self.runner, patch) if patch is not None else self.executor
            run["agent"] = executor.execute(workspace, run_dir, spec, project)
            self._assert_base(workspace, run)
            self.runner.git(workspace, "add", "--all")
            self._check_paths(workspace, project)
            tree_before = self.runner.git(workspace, "write-tree")
            if tree_before == self.runner.git(workspace, "rev-parse", "HEAD^{tree}"):
                raise ApeeError("執行器沒有產生程式變更，未建立待審成果。")
            self.store.transition(task_id, "testing", run=run)
            run["tests"] = self.validator.check(workspace, run_dir, project["tests"], "test")
            self._assert_base(workspace, run)
            self.runner.git(workspace, "add", "--all")
            if self.runner.git(workspace, "write-tree") != tree_before:
                raise ApeeError("測試改動了待交付內容，驗證版本不一致；請檢查產生的檔案。")
            self.runner.git(workspace, "diff", "--cached", "--check")
            self.runner.git(workspace, "diff", "--cached", "--binary", log=run_dir / "changes.patch")
            run["diff_stat"] = self.runner.git(workspace, "diff", "--cached", "--stat")
            self.runner.git(workspace, "-c", "user.name=APEE", "-c", "user.email=apee@localhost",
                            "commit", "-m", f"APEE: {spec['title'][:160]}")
            run["commit"] = self.runner.git(workspace, "rev-parse", "HEAD")
            run["verified_at"] = now()
            (run_dir / "review.md").write_text(self.review_text(run), encoding="utf-8")
            self.store.transition(task_id, "ready", run=run)
        except ExecutorUnavailable as exc:
            run["error"] = str(exc)
            run["executor_error"] = exc.reason
            self.store.defer(task_id, run, exc.reason)
            raise
        except (Exception, KeyboardInterrupt) as exc:
            state = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            run["error"] = str(exc) or "使用者中斷執行。"
            self.store.transition(task_id, state, {"error": run["error"]}, run=run)
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise ApeeError(run["error"]) from exc
        if project["publish_draft"] and not local_only:
            return self.publish(task_id)
        return run

    def _assert_base(self, workspace, run):
        if self.runner.git(workspace, "rev-parse", "HEAD") != run["base_sha"]:
            raise ApeeError("執行器或測試改動了 Git 提交，未繼續交付。")
        if self.runner.git(workspace, "branch", "--show-current") != run["branch"]:
            raise ApeeError("工作分支已改變，未繼續交付。")

    def _check_paths(self, workspace, project, revisions=None):
        allowed = project.get("allowed_paths")
        if allowed is None:
            return
        args = ["git", "diff", "--name-only", "--no-renames", "-z", *(revisions or ["--cached"])]
        changed = self.runner.run(args, cwd=workspace).split("\x00")
        outside = [path for path in changed if path and path not in allowed]
        if outside:
            raise ApeeError("變更超出設定的工作範圍：" + json.dumps(outside, ensure_ascii=False))

    @staticmethod
    def review_text(run):
        spec = run["spec"]
        lines = [f"# {spec['title']}", "", "APEE 已產生程式變更並通過設定的測試，待人工審查。", ""]
        backend = run.get("agent", {}).get("backend")
        if backend == "imported_patch":
            lines += ["修補來源：外部提供的 patch；由 APEE 套用、驗證與交付。本次未由 APEE 的 Codex 執行器產生程式變更。", ""]
        elif backend == "codex":
            lines += ["修補來源：APEE 的 Codex 執行器。", ""]
        if spec.get("url"):
            lines += [f"任務來源：{spec['url']}", ""]
        lines += ["## 工作要求", "", spec["body"], "", "## 變更", "", "```text", run["diff_stat"], "```", "", "## 驗證", ""]
        for result in run["tests"]:
            lines += [f"- 通過：`{json.dumps(result['command'], ensure_ascii=False)}`"]
        lines += ["", f"驗證提交：`{run['commit']}`", f"起始提交：`{run['base_sha']}`",
                  "", "以上結果表示指定的測試命令結束碼為 0；產品行為與程式品質仍需審查。", ""]
        return "\n".join(lines)

    def publish(self, task_id):
        task = self.store.task(task_id)
        self.config.project(task["repo"])
        if task["state"] in ("review", "merged", "closed"):
            return self.store.run(task_id)
        if task["state"] not in ("ready", "publishing"):
            raise ApeeError("只有完成驗證的任務可以交付 Draft PR。")
        run = self.store.run(task_id)
        with self.runner.lease(self.config.state_dir / "runs" / run["id"]):
            return self._publish(task, run)

    def _publish(self, task, run):
        task_id = task["id"]
        workspace = Path(run["workspace"])
        if not run.get("tests") or not all(t.get("passed") is True for t in run["tests"]):
            raise ApeeError("缺少完整測試結果。")
        if self.runner.git(workspace, "rev-parse", "HEAD") != run["commit"] or self.runner.git(workspace, "status", "--porcelain"):
            raise ApeeError("驗證後工作副本已變更，未上傳。")
        self._check_paths(workspace, self.config.project(task["repo"]), [run["base_sha"], run["commit"]])
        self.store.transition(task_id, "publishing", run=run)
        try:
            # Reconcile before any write, including retry after an ambiguous API failure.
            existing = self.github.find_pr(task["repo"], run["branch"])
            if existing:
                if (existing["headRefOid"] != run["commit"] or existing["baseRefName"] != run["base"]
                        or existing["state"] != "OPEN"):
                    raise ApeeError("同名 PR 的版本、目標或狀態不同，請檢查既有 PR。")
                run["pr_url"] = existing["url"]
            else:
                if run["spec"]["source"] == "github":
                    current = self.github.issue(task["repo"], run["spec"]["number"])
                    if not eligible(current, self.config.project(task["repo"])) or current["updated_at"] != run["spec"]["updated_at"]:
                        raise ApeeError("Issue 在執行後已變更或取消派工，未上傳；請檢查任務範圍。")
                self.github.push(task["repo"], workspace, run["branch"], run["commit"])
                body = Path(run["run_dir"]) / "review.md"
                body.write_text(self.review_text(run), encoding="utf-8")
                self.github.create_pr(task["repo"], run["branch"], run["base"], run["spec"]["title"], body)
                confirmed = self.github.find_pr(task["repo"], run["branch"])
                if not confirmed or confirmed["headRefOid"] != run["commit"] or confirmed["baseRefName"] != run["base"] or not confirmed["isDraft"]:
                    raise ApeeError("PR 寫入結果尚未核對成功；再次 publish 會先查詢既有 PR。")
                run["pr_url"] = confirmed["url"]
            run.pop("publish_error", None)
            self.store.transition(task_id, "review", {"url": run["pr_url"]}, run=run)
            return run
        except (Exception, KeyboardInterrupt) as exc:
            run["publish_error"] = str(exc) or "交付被中斷，需核對遠端狀態。"
            self.store.transition(task_id, "publishing", {"error": run["publish_error"]}, run=run)
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise ApeeError(run["publish_error"]) from exc

    def refresh_reviews(self, task_id=None, limit=None):
        if task_id:
            task = self.store.task(task_id)
            self.config.project(task["repo"])
            if task["state"] not in ("review", "merged", "closed"):
                raise ApeeError("只有已交付 PR 的任務可更新追蹤狀態。")
            tasks = [task]
        else:
            tasks = [task for task in self.store.tasks() if task["state"] == "review"
                     and self.config.projects.get(task["repo"], {}).get("enabled")]
        candidates = [(task, self.store.run(task["id"])) for task in tasks]
        candidates.sort(key=lambda pair: (pair[1].get("pr_checked_at", ""), pair[0]["id"]))
        result = {"checked": 0, "changes": [], "errors": []}
        for task, run in candidates[:limit] if limit is not None else candidates:
            run["pr_checked_at"] = now()
            result["checked"] += 1
            try:
                if not run.get("pr_url"):
                    raise ApeeError("交付紀錄缺少 PR 網址。")
                observed = snapshot(self.github.pr_status(task["repo"], run["pr_url"]), run)
            except (ApeeError, OSError, ValueError) as exc:
                message = str(exc)
                changed = run.get("pr_refresh_error", {}).get("message") != message
                run["pr_refresh_error"] = {"message": message, "at": run["pr_checked_at"]}
                if changed:
                    self.store.transition(task["id"], task["state"],
                                          {"event": "pr_refresh_failed", "error": message}, run=run)
                else:
                    self.store.save_run(run)
                result["errors"].append({"task": task["id"], "error": message})
                continue
            changed = run.get("pr_status") != observed or "pr_refresh_error" in run
            run["pr_status"] = observed
            run["pr_observed_at"] = run["pr_checked_at"]
            run.pop("pr_refresh_error", None)
            state = {"OPEN": "review", "MERGED": "merged", "CLOSED": "closed"}[observed["state"]]
            if changed or state != task["state"]:
                description = summary(observed)
                self.store.transition(task["id"], state, {"event": "pr_update", "summary": description}, run=run)
                result["changes"].append({"task": task["id"], "state": state,
                                          "summary": description, "url": run["pr_url"]})
            else:
                self.store.save_run(run)
        return result

    def cycle(self, local_only=False, task_limit=None):
        result = {"added": [], "errors": [], "runs": [], "attempts": 0}
        reviews = self.refresh_reviews(limit=self.config.max_pr_checks)
        result["pr_updates"] = reviews["changes"]
        result["review_checked"] = reviews["checked"]
        result["errors"].extend(reviews["errors"])
        blocker = self.dispatch_blocker()
        if blocker:
            result["blocked"] = blocker
            return result
        synced = self.sync()
        result["added"] = synced["added"]
        result["errors"].extend(synced["errors"])
        limit = min(self.config.max_tasks, task_limit) if task_limit is not None else self.config.max_tasks
        for _ in range(limit):
            blocker = self.dispatch_blocker()
            if blocker:
                result["blocked"] = blocker
                break
            task_id = self.next_task()
            if not task_id:
                break
            try:
                run = self.execute(task_id, local_only)
                result["runs"].append({"task": task_id, "state": self.store.task(task_id)["state"],
                                       "review": str(Path(run["run_dir"]) / "review.md"), "pr_url": run.get("pr_url")})
            except DispatchBlocked as exc:
                result["blocked"] = exc.reason
                break
            except ExecutorUnavailable as exc:
                result["attempts"] += 1
                result["blocked"] = exc.reason
                break
            except ApeeError as exc:
                result["errors"].append({"task": task_id, "error": str(exc)})
            result["attempts"] += 1
        return result
