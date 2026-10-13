import argparse
import json
import signal
import sys
from contextlib import nullcontext
from pathlib import Path

from .config import ApeeError, Config, commands, repo_name
from .engine import Engine
from .process import Runner
from .report import STATES, render, review_description
from .service import Service
from .store import now
from .worker import work


def parser():
    p = argparse.ArgumentParser(description="APEE：GitHub 開發任務與待審成果引擎")
    p.add_argument("--config", default="apee.local.json" if Path("apee.local.json").exists() else "apee.json")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="檢查執行工具與登入狀態")
    discover = sub.add_parser("discover", help="讀取 GitHub 帳號的專案清單")
    discover.add_argument("owner")
    add = sub.add_parser("project-add", help="設定並啟用一個專案")
    add.add_argument("repo")
    add.add_argument("--test", action="append", required=True, help='JSON 命令陣列，例如 ["npm","test"]')
    add.add_argument("--setup", action="append", default=[], help="JSON 依賴準備命令陣列")
    add.add_argument("--setup-network", action="store_true", help="允許依賴準備命令連網")
    add.add_argument("--publish-draft", action="store_true", help="測試通過後自動交付 Draft PR")
    add.add_argument("--priority", type=int, choices=range(1, 6), default=3)
    enqueue = sub.add_parser("enqueue", help="直接加入開發任務")
    enqueue.add_argument("repo")
    enqueue.add_argument("--title", required=True)
    body = enqueue.add_mutually_exclusive_group(required=True)
    body.add_argument("--body")
    body.add_argument("--body-file", type=Path)
    sub.add_parser("sync", help="同步已啟用專案的派工 Issues")
    refresh = sub.add_parser("refresh", help="讀取已交付 PR 的檢查、審查及合併狀態")
    refresh.add_argument("task", nargs="?", help="省略時更新所有啟用專案的待審 PR")
    run = sub.add_parser("run", help="執行指定任務，或佇列中的下一項")
    run.add_argument("task", nargs="?")
    run.add_argument("--local-only", action="store_true", help="保留本機待審成果")
    run.add_argument("--patch", type=Path, help="驗證並交付外部修補，會記錄來源；須指定任務 ID")
    cycle = sub.add_parser("cycle", help="同步任務並推進有限的一輪")
    cycle.add_argument("--local-only", action="store_true")
    worker = sub.add_parser("work", help="有限次數的前景連續執行；不安裝背景服務")
    worker.add_argument("--interval", type=int, default=300, help="每輪完成後等待秒數，預設 300")
    worker.add_argument("--max-cycles", type=int, default=12, help="最多輪數，包含等待輪次，預設 12")
    worker.add_argument("--max-tasks", type=int, default=5, help="本次最多開始幾次嘗試，失敗也計入，預設 5")
    worker.add_argument("--local-only", action="store_true")
    worker.add_argument("--expected-state-dir", type=Path, help="背景服務固定的狀態目錄；變更時停止執行")
    service = sub.add_parser("service", help="管理 macOS 使用者背景服務")
    actions = service.add_subparsers(dest="service_action", required=True)
    for name in ("render", "install"):
        action = actions.add_parser(name)
        action.add_argument("--interval", type=int, default=300, help="排程間隔秒數，至少 60，預設 300")
        action.add_argument("--max-tasks", type=int, default=1, help="每次喚醒最多開始的嘗試數，預設 1")
        action.add_argument("--local-only", action="store_true")
        if name == "render":
            action.add_argument("--output", type=Path, help="另外輸出可檢查的 plist；此指令不登錄服務")
    for name in ("start", "stop", "status", "uninstall"):
        actions.add_parser(name)
    pause = sub.add_parser("pause", help="暫停開始新任務，讓正在執行的任務完成")
    pause.add_argument("--reason", default="使用者暫停派工。")
    resume = sub.add_parser("resume", help="恢復派工；不會直接執行任務")
    resume.add_argument("--codex", action="store_true", help="清除 Codex 暫停原因，將等待中的任務重新入列")
    recover = sub.add_parser("recover", help="檢查未完成嘗試，或將已停止的指定嘗試記為中斷")
    recover.add_argument("task", nargs="?", help="省略時只檢查；指定任務時保留工作副本並恢復狀態")
    recover.add_argument("--legacy", action="store_true", help="僅舊版嘗試：已人工確認所有相關程序結束")
    for command in ("publish", "show", "retry", "cancel"):
        part = sub.add_parser(command)
        part.add_argument("task")
    sub.add_parser("status", help="列出任務進度")
    sub.add_parser("report", help="產生中文總覽")
    return p


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def doctor(config):
    runner = Runner()
    checks = [
        ("Git", ["git", "--version"]), ("GitHub CLI", ["gh", "--version"]),
        ("GitHub 登入", ["gh", "auth", "status"]),
        ("Codex CLI", [config.codex, "--version"]),
        ("Codex 登入", [config.codex, "login", "status"]),
    ]
    failed = False
    for name, args in checks:
        try:
            text = runner.run(args, timeout=30)
            # Do not echo authentication output or tokens into reports.
            print(f"✓ {name}：" + ("可用" if "登入" in name else text.splitlines()[0]))
        except ApeeError:
            failed = True
            print(f"✗ {name}：不可用；請檢查安裝或登入")
    try:
        help_text = runner.run([config.codex, "sandbox", "--help"])
        if "--permission-profile" not in help_text:
            raise ApeeError("缺少 sandbox permission-profile 支援。")
        print("✓ 測試執行介面：支援明確權限設定")
    except ApeeError as exc:
        failed = True
        print(f"✗ {exc}")
    return int(failed)


def add_project(config, args):
    name = repo_name(args.repo)
    if name in config.projects:
        raise ApeeError("專案已有設定，請直接編輯設定檔以保留既有選項。")
    tests = commands([json.loads(s) for s in args.test], "tests", required=True)
    setup = commands([json.loads(s) for s in args.setup], "setup")
    data = dict(config.data)
    data["projects"] = [*data.get("projects", []), dict(
        repo=name, enabled=True, priority=args.priority, issue_label="apee:ready",
        tests=tests, setup=setup, setup_network=args.setup_network, publish_draft=args.publish_draft,
    )]
    temporary = config.path.with_name(config.path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(config.path)
    print(f"已啟用 {name}。設定：{config.path}")


def main(argv=None):
    args = parser().parse_args(argv)
    engine = None
    try:
        if args.command == "service":
            service = Service(args.config)
            if args.service_action in ("render", "install"):
                options = dict(interval=args.interval, max_tasks=args.max_tasks, local_only=args.local_only)
                if args.service_action == "render":
                    emit(service.render(output=args.output, **options))
                else:
                    emit(service.install(**options))
            else:
                emit(getattr(service, args.service_action)())
            return 0
        config = Config(args.config)
        if (args.command == "work" and args.expected_state_dir is not None and
                args.expected_state_dir.expanduser().resolve() != config.state_dir):
            raise ApeeError("背景服務的 state_dir 已變更；請停止原服務後重新安裝，未開始工作。")
        if args.command == "doctor":
            return doctor(config)
        engine = Engine(config)
        # SIGTERM shares the same process-group cleanup path as Ctrl-C.
        def interrupted(_signum, _frame):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, interrupted)
        if args.command == "work":
            status = work(engine, args.interval, args.max_cycles, args.max_tasks, args.local_only, notify=emit)
            return int(status["had_errors"])
        concurrent = args.command in ("status", "show", "report", "pause") or (
            args.command == "resume" and not args.codex
        ) or (
            args.command == "recover" and not args.task
        )
        lock = nullcontext() if concurrent else engine.store.lock()
        with lock:
            if args.command == "pause":
                engine.store.set_control("paused", {"at": now(), "reason": args.reason})
                print("已暫停開始新任務；正在執行的任務仍會完成驗證及原定交付。")
            elif args.command == "resume":
                if args.codex:
                    emit({"requeued": engine.store.resume_codex()})
                engine.store.set_control("paused", None)
                print("已恢復派工設定；連續執行器會在下一輪檢查，也可執行 run 或 cycle。")
            elif args.command == "project-add":
                add_project(config, args)
                engine.config = Config(args.config)
            elif args.command == "discover":
                repos = engine.github.discover(args.owner)
                (config.state_dir / "discovery.json").write_text(json.dumps(repos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(f"已讀取 {len(repos)} 個 repositories。")
            elif args.command == "enqueue":
                body = args.body if args.body is not None else args.body_file.read_text(encoding="utf-8")
                print(engine.enqueue(args.repo, args.title, body))
            elif args.command == "sync":
                result = engine.sync()
                emit(result)
                if result["errors"]:
                    return 1
            elif args.command == "refresh":
                result = engine.refresh_reviews(args.task)
                emit(result)
                if result["errors"]:
                    return 1
            elif args.command == "cycle":
                result = engine.cycle(args.local_only)
                emit(result)
                if result["errors"]:
                    return 1
            elif args.command == "run":
                if args.patch and not args.task:
                    raise ApeeError("提供 --patch 時，必須明確指定任務 ID。")
                task_id = args.task or engine.next_task()
                if not task_id:
                    print("目前沒有可執行的任務。")
                else:
                    run = engine.execute(task_id, args.local_only, args.patch)
                    emit({"task": task_id, "state": engine.store.task(task_id)["state"],
                          "review": str(Path(run["run_dir"]) / "review.md"), "pr_url": run.get("pr_url")})
            elif args.command == "publish":
                print(engine.publish(args.task)["pr_url"])
            elif args.command == "show":
                task = engine.store.task(args.task)
                emit({"task": task, "run": engine.store.run(args.task) if task["run_id"] else None})
            elif args.command == "recover":
                if args.task:
                    emit(engine.recover(args.task, legacy=args.legacy))
                else:
                    if args.legacy:
                        raise ApeeError("--legacy 必須明確指定任務 ID。")
                    emit({"engine_active": engine.store.engine_active(), "attempts": engine.recovery_status()})
            elif args.command == "retry":
                engine.retry(args.task)
                print(f"{args.task}：{STATES['queued']}")
            elif args.command == "cancel":
                task = engine.store.task(args.task)
                if task["state"] not in ("queued", "waiting"):
                    raise ApeeError(f"此操作不適用於 {task['state']} 狀態。")
                engine.store.transition(args.task, "cancelled", {"requested_by": "user"})
                print(f"{args.task}：{STATES['cancelled']}")
            elif args.command == "status":
                attempts = engine.store.attempts_today()
                print(f"今日嘗試：{attempts['used']}/{config.daily_attempt_limit}（{attempts['date']} UTC）")
                print("連續執行：" + ("運作中" if engine.store.worker_active() else "未運作"))
                if not engine.store.engine_active():
                    recovery = engine.recovery_status()
                    if recovery:
                        print(f"需要恢復或核對的嘗試：{len(recovery)}；執行 recover 查看。")
                if engine.store.control("paused"):
                    print("派工已暫停；resume 可恢復。")
                hold = engine.store.control("codex_hold")
                if hold:
                    print(hold["message"])
                tasks = engine.store.tasks()
                for task in tasks:
                    print(f"{STATES.get(task['state'], task['state'])}\t{task['id']}\t{task['spec']['title']}")
                    if task["run_id"]:
                        run = engine.store.run(task["id"])
                        if run.get("pr_url"):
                            print("  " + review_description(run))
                if not tasks:
                    print("目前沒有任務。")
        return 0
    except KeyboardInterrupt:
        print("執行已中斷，成果與進度已保留。", file=sys.stderr)
        return 130
    except (ApeeError, OSError, ValueError) as exc:
        print(f"APEE：{exc}", file=sys.stderr)
        return 1
    finally:
        if engine:
            try:
                print(f"總覽：{render(engine)}")
            except (OSError, ValueError) as exc:
                print(f"總覽更新失敗：{exc}", file=sys.stderr)
            finally:
                engine.store.close()
