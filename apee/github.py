"""GitHub access through the user's existing gh login."""

import json
import re
import hashlib

from .config import ApeeError, repo_name
from .reviews import pr_number
from .quality import diagnostic_text


class CINotStarted(ApeeError):
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        super().__init__('GitHub 在執行測試前拒絕 CI 工作，未開始程式修復。'+diagnostics)


class GitHub:
    def __init__(self, runner):
        self.runner = runner

    def json(self, args):
        try:
            return json.loads(self.runner.run(["gh", *args]))
        except ValueError as exc:
            raise ApeeError("GitHub 回傳無效 JSON。") from exc

    def discover(self, owner):
        repo_name(owner + "/probe")
        return self.json([
            "repo", "list", owner, "--limit", "1000", "--json",
            "nameWithOwner,description,isArchived,isFork,updatedAt,primaryLanguage",
        ])

    def issues(self, project):
        pages = self.json([
            "api", "--hostname", "github.com", "--method", "GET", "--paginate", "--slurp",
            f"repos/{project['repo']}/issues", "-f", "state=open", "-f", "per_page=100",
            "-f", "sort=created", "-f", "direction=asc", "-f", f"labels={project['issue_label']}",
        ])
        return [issue for page in pages for issue in page if "pull_request" not in issue]

    def issue(self, repo, number):
        return self.json(["api", "--hostname", "github.com", f"repos/{repo}/issues/{number}"])

    def metadata(self, repo):
        data = self.json(["api", "--hostname", "github.com", f"repos/{repo}"])
        if data["archived"]:
            raise ApeeError("此 repository 已封存。")
        if not data.get("default_branch"):
            raise ApeeError("此 repository 尚無預設分支。")
        return data

    def clone(self, repo, workspace, base):
        self.runner.run([
            "gh", "repo", "clone", repo, str(workspace), "--", "--single-branch",
            "--branch", base, "--config", "core.hooksPath=/dev/null",
        ], timeout=300)

    def find_pr(self, repo, branch):
        prs = self.json([
            "pr", "list", "--repo", repo, "--head", branch, "--state", "all", "--limit", "100",
            "--json", "url,state,headRefName,headRefOid,baseRefName,isDraft",
        ])
        return next((pr for pr in prs if pr["headRefName"] == branch), None)

    def pr_status(self, repo, url):
        number = pr_number(repo, url)
        return self.json([
            "pr", "view", str(number), "--repo", repo, "--json",
            "url,state,isDraft,headRefOid,headRefName,baseRefName,reviewDecision,"
            "mergeStateStatus,statusCheckRollup,mergedAt,closedAt",
        ])

    def push(self, repo, workspace, branch, commit):
        url = f"https://github.com/{repo}.git"
        credentials = ["-c", "credential.helper=", "-c", "credential.https://github.com.helper=!gh auth git-credential"]
        remote = self.runner.git(workspace, *credentials, "ls-remote", url, f"refs/heads/{branch}")
        if remote and remote.split()[0] != commit:
            raise ApeeError("遠端任務分支已有不同內容，請先檢查；未覆寫。")
        self.runner.git(workspace, *credentials, "push", "--", url, f"{commit}:refs/heads/{branch}", timeout=300)

    def create_pr(self, repo, branch, base, title, body_path):
        return self.runner.run([
            "gh", "pr", "create", "--repo", repo, "--draft", "--head", branch,
            "--base", base, "--title", title[:200], "--body-file", str(body_path),
        ]).strip()

    def failure_logs(self, repo, url, head):
        number = pr_number(repo, url)
        observed = self.pr_status(repo, url)
        if observed['state'] != 'OPEN' or observed['headRefOid'] != head:
            raise ApeeError('PR 已關閉或版本改變，未讀取修復來源。')
        result, ids = [], set()
        for check in observed.get('statusCheckRollup') or []:
            if check.get('conclusion') not in ('FAILURE', 'TIMED_OUT'):
                continue
            match = re.fullmatch(r'https://github\.com/' + re.escape(repo) + r'/actions/runs/([1-9][0-9]*)(?:/job/[0-9]+)?',
                                 check.get('detailsUrl') or '', flags=re.IGNORECASE)
            if not match:
                continue
            identifier = int(match[1])
            if identifier in ids or len(ids) >= 5:
                continue
            ids.add(identifier)
            run = self.json(['api', '--hostname', 'github.com', f'repos/{repo}/actions/runs/{identifier}'])
            associations = run.get('pull_requests') or []
            matches = run.get('head_sha') == head or any(
                item.get('number') == number and item.get('head', {}).get('sha') == head for item in associations)
            if (not matches or run.get('status') != 'completed'
                    or run.get('conclusion') not in ('failure', 'timed_out')
                    or run.get('repository', {}).get('full_name', '').lower() != repo.lower()):
                raise ApeeError('CI 執行版本或來源無法核對。')
            try:
                raw = self.runner.run(['gh', 'run', 'view', str(identifier), '--repo', repo, '--log-failed'], timeout=120)
            except ApeeError as original:
                jobs = self.json(['api', '--hostname', 'github.com', f'repos/{repo}/actions/runs/{identifier}/jobs?per_page=100'])
                for job in (jobs.get('jobs') or [])[:100]:
                    if job.get('conclusion') not in ('failure', 'timed_out') or job.get('steps') or job.get('runner_id') != 0: continue
                    check_url = job.get('check_run_url', '')
                    check_id = re.fullmatch(r'https://api\.github\.com/repos/'+re.escape(repo)+r'/check-runs/([1-9][0-9]*)', check_url, re.IGNORECASE)
                    if not check_id or job.get('run_id') != identifier: continue
                    annotations = self.json(['api', '--hostname', 'github.com', f'repos/{repo}/check-runs/{check_id[1]}/annotations'])
                    messages = [item.get('message', '') for item in annotations if item.get('annotation_level')=='failure']
                    if messages: raise CINotStarted(diagnostic_text('\n'.join(messages))) from original
                raise
            if raw.strip():
                result.append(dict(run_id=identifier, url=run['html_url'], head=head,
                                   digest=hashlib.sha256(raw.encode()).hexdigest(), text=diagnostic_text(raw)))
        if not result:
            raise ApeeError('沒有可核對的 GitHub Actions 失敗記錄；保留 PR 待處理。')
        return result

    def push_update(self, repo, workspace, branch, previous, commit):
        # Only an append to the exact observed branch is allowed, even if it moves between calls.
        self.runner.git(workspace, 'merge-base', '--is-ancestor', previous, commit)
        url = f'https://github.com/{repo}.git'
        credentials = ['-c', 'credential.helper=', '-c', 'credential.https://github.com.helper=!gh auth git-credential']
        remote = self.runner.git(workspace, *credentials, 'ls-remote', url, 'refs/heads/' + branch)
        if not remote or remote.split()[0] not in (previous, commit):
            raise ApeeError('遠端 PR 分支已改變或刪除，未更新。')
        if remote.split()[0] == commit:
            return
        self.runner.git(workspace, *credentials, 'push', '--force-with-lease=refs/heads/' + branch + ':' + previous,
                        '--', url, commit + ':refs/heads/' + branch, timeout=300)
