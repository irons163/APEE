"""Read-only PR observations, kept separate from the original local test evidence."""

import json
import re

from .config import ApeeError, repo_name


CHECK_LABELS = {
    "none": "未回報檢查", "unknown": "檢查狀態不明", "pending": "檢查執行中／等待中",
    "passed": "已回報檢查通過", "failed": "檢查失敗", "cancelled": "檢查已取消",
    "skipped": "檢查皆略過／中立",
}


def pr_number(repo, url):
    match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/([1-9][0-9]*)", str(url))
    if not match or repo_name(match[1]) != repo_name(repo):
        raise ApeeError("PR 網址不屬於設定的 GitHub repository。")
    return int(match[2])


def check_result(node):
    if not isinstance(node, dict):
        return {"name": "未知檢查", "bucket": "unknown", "status": "", "conclusion": "", "url": ""}
    kind = node.get("__typename")
    status = str(node.get("status") or node.get("state") or "").upper()
    conclusion = str(node.get("conclusion") or "").upper()
    bucket = "unknown"
    if kind == "CheckRun":
        if status in ("REQUESTED", "QUEUED", "IN_PROGRESS", "WAITING", "PENDING"):
            bucket = "pending"
        elif status == "COMPLETED":
            if conclusion == "SUCCESS":
                bucket = "passed"
            elif conclusion in ("SKIPPED", "NEUTRAL"):
                bucket = "skipped"
            elif conclusion == "CANCELLED":
                bucket = "cancelled"
            elif conclusion in ("FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"):
                bucket = "failed"
    elif kind == "StatusContext":
        bucket = {"SUCCESS": "passed", "FAILURE": "failed", "ERROR": "failed",
                  "EXPECTED": "pending", "PENDING": "pending"}.get(status, "unknown")
    return {"name": str(node.get("name") or node.get("context") or "未知檢查"),
            "bucket": bucket, "status": status, "conclusion": conclusion,
            "url": str(node.get("detailsUrl") or node.get("targetUrl") or "")}


def snapshot(data, run):
    if not isinstance(data, dict):
        raise ApeeError("GitHub PR 資料格式不完整。")
    expected = pr_number(run["repo"], run["pr_url"])
    if pr_number(run["repo"], data.get("url")) != expected:
        raise ApeeError("GitHub 回傳的 PR 與原交付不一致。")
    if (data.get("state") not in ("OPEN", "MERGED", "CLOSED")
            or type(data.get("isDraft")) is not bool
            or any(not isinstance(data.get(key), str) or not data[key]
                   for key in ("headRefOid", "headRefName", "baseRefName"))):
        raise ApeeError("GitHub PR 缺少狀態、版本或分支資料。")
    rollup = data.get("statusCheckRollup")
    if rollup is not None and not isinstance(rollup, list):
        raise ApeeError("GitHub 檢查結果格式不完整。")
    if any(data.get(key) is not None and not isinstance(data[key], str)
           for key in ("reviewDecision", "mergeStateStatus", "mergedAt", "closedAt")):
        raise ApeeError("GitHub 審查狀態格式不完整。")
    checks = sorted((check_result(item) for item in (rollup or [])),
                    key=lambda item: json.dumps(item, sort_keys=True))
    buckets = [item["bucket"] for item in checks]
    ci = "unknown" if rollup is None else "none"
    for candidate in ("failed", "cancelled", "pending", "unknown", "passed", "skipped"):
        if candidate in buckets:
            ci = candidate
            break
    result = {
        "state": data["state"], "is_draft": data["isDraft"], "url": run["pr_url"],
        "head": data["headRefOid"], "branch": data["headRefName"], "base": data["baseRefName"],
        "review": data.get("reviewDecision") or "", "merge_status": data.get("mergeStateStatus") or "UNKNOWN",
        "merged_at": data.get("mergedAt"), "closed_at": data.get("closedAt"), "ci": ci, "checks": checks,
        "matches_verified_commit": data["headRefOid"] == run["commit"],
        "matches_delivery_target": data["baseRefName"] == run["base"] and data["headRefName"] == run["branch"],
    }
    result["attention"] = attention(result)
    return result


def attention(status):
    reasons = []
    if not status["matches_verified_commit"]:
        reasons.append("PR 版本已改變，原本機測試不涵蓋目前版本")
    if not status["matches_delivery_target"]:
        reasons.append("PR 分支或合併目標已改變")
    if status["state"] == "OPEN":
        if status["review"] == "CHANGES_REQUESTED":
            reasons.append("審查者要求修改")
        if status["merge_status"] == "DIRTY":
            reasons.append("PR 有合併衝突")
        if status["ci"] in ("failed", "cancelled", "unknown"):
            reasons.append(CHECK_LABELS[status["ci"]])
    return reasons


def summary(status):
    state = {"OPEN": "草稿待審" if status["is_draft"] else "待審查",
             "MERGED": "已合併", "CLOSED": "已關閉（未合併）"}[status["state"]]
    review = {"APPROVED": "審查已核准", "CHANGES_REQUESTED": "審查要求修改",
              "REVIEW_REQUIRED": "等待審查"}.get(status["review"], "尚無審查結論")
    parts = [state, CHECK_LABELS[status["ci"]], review]
    if status["attention"]:
        parts += status["attention"]
    return "；".join(dict.fromkeys(parts))
