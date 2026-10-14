"""One-task-at-a-time GitHub development, validation and review handoff."""

import hashlib
import json
import uuid
from pathlib import Path
from datetime import datetime

from .budget import Budget
from . import ci
from .config import ApeeError, Config, DispatchBlocked
from .executors import CodexExecutor, ExecutorUnavailable, PatchExecutor, Validator
from .github import GitHub
from .leases import attempt_lease, lease_state
from .process import Runner
from .quality import CodexReviewer, ValidationFailed, diagnostic_text, model_call, validate_review
from .portfolio import Portfolio
from .planning import CodexPlanner
from . import planning
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
    def __init__(self, config, runner=None, github=None, executor=None, validator=None, reviewer=None, planner=None):
        self.config = config
        self.runner = runner or Runner()
        self.github = github or GitHub(self.runner)
        self.executor = executor or CodexExecutor(config, self.runner)
        self.validator = validator or Validator(config, self.runner)
        self.reviewer = reviewer or CodexReviewer(config, self.runner)
        self.planner = planner or CodexPlanner(config, self.runner)
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
        if isinstance(self.reviewer, CodexReviewer):
            self.reviewer.config = config
        if isinstance(self.planner, CodexPlanner):
            self.planner.config = config

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
        planning_jobs = [job['id'] for job in self.store.model_jobs()
                         if lease_state(job['run_dir']) in ('active', 'unreadable') or job['state']=='running']
        if planning_jobs:
            return dict(kind='planning_active', jobs=planning_jobs, message='尚有未完成的規劃回合；核對程序與用量後才能開始新工作。')
        hold = self.store.control("codex_hold") if use_codex else None
        if hold:
            return hold
        budget = Budget(self.config, self.store).blocker() if use_codex else None
        if budget:
            return budget
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

    def recover(self, task_id, legacy=False, automatic=False, observation=None):
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
            if automatic:
                run["recovery"]["source"] = "automatic"
                run["recovery"]["observation"] = observation
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

    def auto_recover(self, at=None):
        policy = self.config.auto_recovery
        if not policy['enabled'] or self.store.control('paused'):
            if self.store.control('recovery_observations'):
                self.store.set_control('recovery_observations', None)
            return []
        at = at or now()
        instant = datetime.fromisoformat(at)
        observations = self.store.control('recovery_observations') or {}
        kept, results = {}, []
        for task in self.store.tasks():
            if len(results) >= policy['max_per_cycle']:
                if task['run_id'] in observations:
                    kept[task['run_id']] = observations[task['run_id']]
                continue
            if not task['run_id'] or not self.config.projects.get(task['repo'], {}).get('enabled'):
                continue
            run = self.store.run(task['id'])
            if (type(run.get('lease_version')) is not int or run['lease_version'] != 1
                    or run.get('execution_backend') != 'codex'):
                continue
            run_dir = self.config.state_dir / 'runs' / run['id']
            if run['run_dir'] != str(run_dir) or run['workspace'] != str(run_dir / 'repo'):
                continue
            if any(lease_state(self.config.state_dir / 'runs' / old['id']) in ('active', 'unreadable')
                   for old in self.store.runs(task['id'])):
                continue
            recovered = False
            if task['state'] in RECOVERABLE_STATES:
                if lease_state(run_dir) != 'idle':
                    continue
                first = observations.get(run['id'], at)
                try:
                    elapsed = (instant - datetime.fromisoformat(first)).total_seconds()
                except (ValueError, TypeError):
                    first, elapsed = at, 0
                if elapsed < 0:
                    first, elapsed = at, 0
                if elapsed < policy['cooldown_seconds']:
                    kept[run['id']] = first
                    continue
                try:
                    self.recover(task['id'], automatic=True,
                                 observation=dict(idle_since=first, cooldown_seconds=policy['cooldown_seconds'], checked_at=at))
                except (BusyError, OSError):
                    continue
                run = self.store.run(task['id'])
                task = self.store.task(task['id'])
                recovered = True
            if task['state'] != 'interrupted' or run.get('recovery', {}).get('source') != 'automatic':
                continue
            if run.get('automatic_retry', {}).get('scheduled'):
                continue
            count = sum(bool(old.get('automatic_retry', {}).get('scheduled')) for old in self.store.runs(task['id']))
            allowed = (count < policy['max_retries'] and not self.store.control('codex_hold')
                       and not Budget(self.config, self.store).blocker()
                       and self.store.attempts_today()['used'] < self.config.daily_attempt_limit)
            if allowed:
                try:
                    with attempt_lease(run_dir, create=False):
                        run['automatic_retry'] = dict(scheduled=True, number=count + 1, at=at)
                        self.store.transition(task['id'], 'queued',
                                              dict(reason='automatic_recovery', run_id=run['id'], retry_number=count + 1), run=run)
                except (BusyError, OSError):
                    allowed = False
            if recovered or allowed:
                results.append(dict(task=task['id'], run_id=run['id'], retried=bool(allowed),
                                    state='queued' if allowed else 'interrupted'))
        if kept != observations:
            self.store.set_control('recovery_observations', kept or None)
        return results

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
        return Portfolio(self).decisions()['next_task']

    def execute(self, task_id, local_only=False, patch=None):
        task = self.store.task(task_id)
        project = self.config.project(task["repo"])
        if task["state"] != "queued":
            raise ApeeError(f"任務狀態是 {task['state']}，不會重複執行。")
        decision = next(item for item in Portfolio(self).decisions()['tasks'] if item['task']==task_id)
        self.store.set_control('dispatch_decision', dict(decision, at=now()))
        if not decision['eligible']:
            raise ApeeError('任務尚不能開始：'+'；'.join(decision['reasons']))
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
                   branch=f"apee/task-{run_id[:16]}", lease_version=1,
                   execution_backend='imported_patch' if patch is not None else 'codex',
                   quality_required=self.config.quality['enabled'] or spec['source'] == 'ci_repair')
        if spec['source'] == 'ci_repair':
            run['branch'] = spec['branch']
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
            context = Portfolio(self).links().get(task_id)
            if context and context.get('goal'):
                goal = self.store.document('goals', context['goal'])
                run['goal_context'] = dict(goal=goal, depends_on=context['depends_on'])
                spec = run['spec'] = dict(spec, body=spec['body']+'\n\n所屬目標：'+goal['objective']+'\n目標完成條件：'+goal['criteria'])
            if spec['source'] == 'ci_repair':
                ci.assert_source(self, spec, task['repo'])
                run['base'] = spec['base']
                self.github.clone(task['repo'], workspace, run['branch'])
            else:
                meta = self.github.metadata(task["repo"])
                run["base"] = project.get('base_branch') or meta["default_branch"]
                self.github.clone(task["repo"], workspace, run["base"])
            run["base_sha"] = self.runner.git(workspace, "rev-parse", "HEAD")
            if spec['source'] == 'ci_repair':
                if run['base_sha'] != spec['expected_head']:
                    raise ApeeError('CI 修復 checkout 的版本已改變。')
            else:
                self.runner.git(workspace, "checkout", "-b", run["branch"])
            self.validator.check(workspace, run_dir, project["setup"], "setup", project.get("setup_network", False))
            if self.runner.git(workspace, "status", "--porcelain"):
                raise ApeeError("依賴準備改動了版本控制內容；請先修正 setup 設定。")
            self.store.transition(task_id, "running", run=run)
            executor = PatchExecutor(self.runner, patch) if patch is not None else self.executor
            budget = Budget(self.config, self.store)
            if patch is None:
                budget.begin(run)
            try:
                run["agent"] = executor.execute(workspace, run_dir, spec, project)
            finally:
                if patch is None:
                    budget.finish(run)
            self._assert_base(workspace, run)
            if run['quality_required']:
                self._verify_quality(run, project)
            else:
                self._verify_tests(run, project)
            self.runner.git(workspace, "diff", "--cached", "--check")
            self.runner.git(workspace, "diff", "--cached", "--binary", log=run_dir / "changes.patch")
            run["diff_stat"] = self.runner.git(workspace, "diff", "--cached", "--stat")
            self.runner.git(workspace, "-c", "user.name=APEE", "-c", "user.email=apee@localhost",
                            "commit", "-m", f"APEE: {spec['title'][:160]}")
            run["commit"] = self.runner.git(workspace, "rev-parse", "HEAD")
            run["verified_at"] = now()
            (run_dir / "review.md").write_text(self.review_text(run), encoding="utf-8")
            self.store.transition(task_id, "ready", run=run)
        except DispatchBlocked as exc:
            run['error'] = str(exc)
            run['deferred'] = exc.reason
            self.store.transition(task_id, 'waiting', exc.reason, run=run)
            raise
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

    def _candidate_tree(self, run, project):
        workspace = Path(run['workspace'])
        self._assert_base(workspace, run)
        self.runner.git(workspace, "add", "--all")
        self._check_paths(workspace, project)
        tree_before = self.runner.git(workspace, "write-tree")
        if tree_before == self.runner.git(workspace, "rev-parse", "HEAD^{tree}"):
            raise ApeeError("執行器沒有產生程式變更，未建立待審成果。")
        return tree_before

    def _verify_tests(self, run, project, directory=None):
        workspace = Path(run['workspace'])
        tree_before = self._candidate_tree(run, project)
        self.store.transition(run['task_id'], "testing", run=run)
        error = None
        try:
            run["tests"] = self.validator.check(workspace, directory or Path(run['run_dir']), project["tests"], "test")
        except ApeeError as exc:
            error = exc
        self._assert_base(workspace, run)
        self.runner.git(workspace, "add", "--all")
        if self.runner.git(workspace, "write-tree") != tree_before:
            raise ApeeError("測試改動了待交付內容，驗證版本不一致；請檢查產生的檔案。")
        if error:
            raise ValidationFailed(str(error)) from error
        return tree_before

    def _verify_quality(self, run, project):
        run['verification_rounds'] = []
        for index in range(self.config.quality['max_repairs'] + 1):
            directory = Path(run['run_dir']) / 'quality' / str(index)
            directory.mkdir(parents=True, exist_ok=True)
            observation = dict(round=index, at=now(), tests_passed=False)
            failure = None
            try:
                tree = self._verify_tests(run, project, directory)
                observation.update(tree=tree, tests_passed=True)
            except ValidationFailed as exc:
                failure = dict(kind='tests', message=str(exc), logs=[dict(name=p.name, text=diagnostic_text(p.read_text(errors='replace')))
                              for p in sorted(directory.glob('*.log'))[:5]])
                observation['failure'] = failure
            if failure is None:
                review = model_call(self, run, f'review-{index}',
                    lambda folder: self.reviewer.review(Path(run['workspace']), folder, run['spec'], project, run['base_sha']))
                validate_review({key: review.get(key) for key in ('approved', 'summary', 'findings')},
                                Path(run['workspace']), run['base_sha'], self.runner)
                self._assert_base(Path(run['workspace']), run)
                self.runner.git(Path(run['workspace']), 'add', '--all')
                if self.runner.git(Path(run['workspace']), 'write-tree') != tree:
                    raise ApeeError('獨立審查改動了內容，未交付。')
                review.update(tree=tree, round=index)
                run['code_review'] = review
                observation['review'] = review
                if review['approved']:
                    run['verification_rounds'].append(observation)
                    self.store.save_run(run)
                    return
                failure = dict(kind='code_review', findings=review['findings'], summary=review['summary'])
                observation['failure'] = failure
            run['verification_rounds'].append(observation)
            self.store.save_run(run)
            if index >= self.config.quality['max_repairs']:
                raise ApeeError('測試或獨立審查未通過，已達有限修復上限；保留成果待處理。')
            repair = dict(run['spec'], body=run['spec']['body'] + '\n\n修復以下已核對問題，保留原完成條件與測試，不得削弱檢查：\n'
                          + json.dumps(failure, ensure_ascii=False))
            self.store.transition(run['task_id'], 'running', run=run)
            result = model_call(self, run, f'repair-{index + 1}',
                                lambda folder: self.executor.execute(Path(run['workspace']), folder, repair, project))
            run.setdefault('repairs', []).append(dict(round=index + 1, agent=result, at=now()))
            self.store.save_run(run)

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
            if run.get('repairs'):
                lines += [f"初始修補來源：外部 patch；最終內容另包含 {len(run['repairs'])} 次 Codex 有限修復。各回合的來源、用量與驗證分開保存。", '']
            else:
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
        if run.get('quality_required'):
            review = run['code_review']
            lines += ['## 獨立模型審查', '', review['summary'], '',
                      f"審查內容樹：`{review['tree']}`；已執行 {len(run.get('repairs', []))} 次有限修復。", '']
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

    def check_delivery(self, task, run):
        """Recheck local evidence before either publication or explicit local acceptance."""
        workspace = Path(run["workspace"])
        if not run.get("tests") or not all(t.get("passed") is True for t in run["tests"]):
            raise ApeeError("缺少完整測試結果。")
        if run.get('quality_required'):
            review = run.get('code_review', {})
            if (review.get('approved') is not True or any(finding['priority'] <= 2 for finding in review.get('findings', []))
                    or review.get('tree') != self.runner.git(workspace, 'rev-parse', run['commit'] + '^{tree}')):
                raise ApeeError('缺少對應內容版本的獨立審查，未交付。')
        if self.runner.git(workspace, "rev-parse", "HEAD") != run["commit"] or self.runner.git(workspace, "status", "--porcelain"):
            raise ApeeError("驗證後工作副本已變更，未上傳。")
        self._check_paths(workspace, self.config.project(task["repo"]), [run["base_sha"], run["commit"]])

    def _publish(self, task, run):
        task_id = task["id"]
        workspace = Path(run['workspace'])
        self.check_delivery(task, run)
        self.store.transition(task_id, "publishing", run=run)
        try:
            if run['spec']['source'] == 'ci_repair':
                return ci.publish(self, task, run)
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
        from .notifications import Notifications
        Notifications(self).collect()
        result = {"added": [], "errors": [], "runs": [], "attempts": 0}
        reviews = self.refresh_reviews(limit=self.config.max_pr_checks)
        result["pr_updates"] = reviews["changes"]
        result["review_checked"] = reviews["checked"]
        result["errors"].extend(reviews["errors"])
        Budget(self.config, self.store).reconcile()
        Portfolio(self).measure()
        planning.recover_jobs(self)
        recovered = self.auto_recover()
        if recovered:
            result['recoveries'] = recovered
        blocker = self.dispatch_blocker()
        if blocker:
            result["blocked"] = blocker
            return result
        synced = self.sync()
        result["added"] = synced["added"]
        result["errors"].extend(synced["errors"])
        repairs = ci.schedule(self)
        result['added'].extend(repairs['added'])
        result['errors'].extend(repairs['errors'])
        proposed = planning.cycle(self)
        result['planning'] = proposed
        result['added'].extend(proposed['enqueued'])
        if proposed.get('error'):
            result['errors'].append(dict(stage='planning', error=proposed['error']))
        limit = min(self.config.max_tasks, task_limit) if task_limit is not None else self.config.max_tasks
        for _ in range(limit):
            blocker = self.dispatch_blocker()
            if blocker:
                result["blocked"] = blocker
                break
            task_id = self.next_task()
            if not task_id:
                break
            before = self.store.total_attempts()
            try:
                run = self.execute(task_id, local_only)
                result["runs"].append({"task": task_id, "state": self.store.task(task_id)["state"],
                                       "review": str(Path(run["run_dir"]) / "review.md"), "pr_url": run.get("pr_url")})
            except DispatchBlocked as exc:
                result["blocked"] = exc.reason
                break
            except ExecutorUnavailable as exc:
                result["blocked"] = exc.reason
                break
            except ApeeError as exc:
                result["errors"].append({"task": task_id, "error": str(exc)})
            finally:
                result['attempts'] += self.store.total_attempts() - before
        return result
