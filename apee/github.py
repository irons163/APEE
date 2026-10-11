"""GitHub access through the user's existing gh login."""

import json

from .config import ApeeError, repo_name
from .reviews import pr_number


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
