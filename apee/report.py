import json
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .reviews import summary


STATES = {
    "queued": "待執行", "preparing": "準備中", "running": "開發中", "testing": "測試中",
    "ready": "本機待審", "publishing": "交付中／需核對", "review": "PR 待審",
    "failed": "失敗／需處理", "interrupted": "已中斷", "cancelled": "已取消", "waiting": "等待 Codex 恢復",
    "merged": "PR 已合併", "closed": "PR 已關閉（未合併）",
}


def cell(value):
    return str(value or "—").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def review_description(run):
    status = run.get("pr_status")
    description = summary(status) if status else "尚未讀取 PR 近況；執行 refresh 可更新"
    if status:
        description += f"（截至 {run['pr_observed_at']}）"
    if run.get("pr_refresh_error"):
        description = "更新失敗，以下為上次資訊：" + description
    return description


def render(engine):
    stamp = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M %Z")
    lines = ["# APEE 專案工作總覽", "", f"更新時間：{stamp}", "",
             "工作流程：取得任務 → 獨立工作副本 → 自動開發 → 指定測試 → 待審變更。", ""]
    configured = [p for p in engine.config.projects.values() if p["enabled"]]
    lines += [f"目前啟用 {len(configured)} 個專案；每輪最多處理 {engine.config.max_tasks} 個任務。", ""]
    attempts = engine.store.attempts_today()
    active = engine.store.worker_active()
    lines += [f"今日已開始 {attempts['used']}／{engine.config.daily_attempt_limit} 次嘗試（{attempts['date']} UTC）。",
              "連續執行：" + ("運作中。" if active else "未運作。"), ""]
    paused, hold = engine.store.control("paused"), engine.store.control("codex_hold")
    if paused:
        lines += ["**已暫停派工**：" + cell(paused["reason"]), ""]
    if hold:
        lines += ["**需要處理**：" + cell(hold["message"]),
                  f"錯誤記錄：[agent-events.log](<{hold['log']}>)", ""]
    if attempts["used"] >= engine.config.daily_attempt_limit:
        lines += ["**已達每日嘗試上限**：下個 UTC 日期才會恢復派工。", ""]
    worker = engine.store.control("worker")
    if worker:
        lines += [f"最近連續執行：已檢查 {worker['cycles']} 輪，開始 {worker['attempts']} 次嘗試；"
                  f"最後更新 {worker.get('updated_at', worker['started_at'])}。", ""]
    tasks = engine.store.tasks()
    runs = {task["id"]: engine.store.run(task["id"]) for task in tasks if task["run_id"]}
    recovery = {}
    if not engine.store.engine_active():
        for item in engine.recovery_status():
            previous = recovery.get(item["task"])
            if previous is None or item["lease"] in ("active", "unreadable"):
                recovery[item["task"]] = item
    attention = []
    for task in tasks:
        run = runs.get(task["id"], {})
        reasons = list(run.get("pr_status", {}).get("attention", []))
        if run.get("pr_refresh_error"):
            reasons.insert(0, "無法更新 PR 狀態：" + run["pr_refresh_error"]["message"])
        if task["id"] in recovery:
            item = recovery[task["id"]]
            if item["lease"] == "active":
                reasons.append("殘留程序仍持有執行鎖；先讓程序結束，再 recover 檢查。")
            elif item["action"] == "publish":
                reasons.append("交付結果需核對；使用 publish 核對既有 PR。")
            elif item["action"] == "recover":
                reasons.append("原執行已停止；使用 recover 記為中斷，保留工作副本。")
            else:
                reasons.append("執行鎖缺失或無法讀取；需要人工檢查未完成嘗試。")
        if reasons:
            attention.append((task, run, reasons))
    if attention:
        lines += ["## 需要你處理", "", "| 任務 | 原因 | 成果 |", "| --- | --- | --- |"]
        for task, run, reasons in attention:
            link = f"[查看 PR]({run['pr_url']})" if run.get("pr_url") else "—"
            lines += [f"| {cell(task['id'])} | {cell('；'.join(reasons))} | {link} |"]
        lines += [""]
    if tasks:
        lines += ["## 任務", "", "| 專案／任務 | 狀態 | 成果 | GitHub 近況 |", "| --- | --- | --- | --- |"]
        for task in tasks:
            output = "—"
            progress = "—"
            if task["run_id"]:
                run = runs[task["id"]]
                if run.get("pr_url"):
                    label = "Draft PR" if run.get("pr_status", {}).get("is_draft", True) else "PR"
                    output = f"[{label}]({run['pr_url']})"
                    progress = cell(review_description(run))
                elif (engine.config.state_dir / "runs" / run["id"] / "review.md").exists():
                    output = f"[審查報告](<{run['run_dir']}/review.md>)"
                elif run.get("error"):
                    output = cell(run["error"])
            lines += [f"| {cell(task['id'])} — {cell(task['spec']['title'])} | {STATES.get(task['state'], task['state'])} | {output} | {progress} |"]
    else:
        lines += ["尚未排入開發任務。啟用專案並設定測試後，可加入直接任務，或同步具有派工標籤的 GitHub Issues。", ""]
    discovery = engine.config.state_dir / "discovery.json"
    if discovery.exists():
        repos = json.loads(discovery.read_text(encoding="utf-8"))
        candidates = [r for r in repos if not r["isArchived"] and not r["isFork"]]
        lines += ["", "## GitHub 專案來源", "", f"上次讀取到 {len(repos)} 個 repositories，其中 {len(candidates)} 個不是 fork、也未封存。",
                  "下表列出最近更新的 25 個候選專案。更新時間僅供尋找專案，不代表工作優先順序。",
                  "", "| Repository | 主要語言 | GitHub 更新時間（UTC） |", "| --- | --- | --- |"]
        for repo in sorted(candidates, key=lambda r: r["updatedAt"], reverse=True)[:25]:
            name = repo["nameWithOwner"]
            language = (repo.get("primaryLanguage") or {}).get("name", "—")
            lines += [f"| [{cell(name)}](https://github.com/{name}) | {cell(language)} | {cell(repo['updatedAt'])} |"]
        lines += ["", "來源清單僅供選擇專案；實際執行範圍由設定中的 enabled 專案決定。",
                  f"完整資料：[discovery.json](<{discovery}>)", ""]
    lines += ["", "## 本版邊界", "", "- 每輪依專案優先順序及入列時間，循序執行有限數量的任務。",
              "- 模型結束後，由引擎另外執行設定的測試；通過才準備待審成果。",
              "- Draft PR 可依專案設定自動交付；合併、發佈與部署仍由既有流程負責。",
              "- cycle／work 每輪讀取 PR 近況；模型派工暫停時仍可更新。status／report 顯示上次觀察，不會連網。",
              "- 未回報檢查不算通過；GitHub 狀態與本機驗證分開保存。已合併表示 PR 狀態，不表示產品成效。",
              "- work 提供有輪數與任務上限的前景連續模式；尚未安裝背景常駐服務。",
              "- 用量或登入問題會暫停 Codex 派工，保存原因，等待明確恢復；不會自動重試開發失敗。",
              "- 每項任務分別保存開發、測試與遠端交付結果，方便核對實際完成的階段。", ""]
    path = engine.config.state_dir / "overview.md"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".overview-", delete=False) as output:
        temporary = Path(output.name)
        output.write("\n".join(lines))
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
