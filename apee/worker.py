"""Bounded foreground worker. Never installs a service or retries failed jobs implicitly."""

import os
import json
import time

from .config import BusyError, positive_int
from .report import render
from .store import now


def work(engine, interval=300, max_cycles=12, max_tasks=5, local_only=False,
         sleep=time.sleep, notify=None):
    positive_int(interval, "interval")
    positive_int(max_cycles, "max_cycles", 10000)
    positive_int(max_tasks, "max_tasks", 1000)
    notify = notify or (lambda value: None)
    with engine.store.lock("worker"):
        status = dict(pid=os.getpid(), started_at=now(), state="running", cycles=0, attempts=0,
                      max_cycles=max_cycles, max_tasks=max_tasks, interval=interval,
                      local_only=local_only, had_errors=False)
        engine.store.set_control("worker", status)
        notify({"worker": "started", "max_cycles": max_cycles, "max_tasks": max_tasks})
        previous_idle = None
        previous_errors = None
        try:
            for _ in range(max_cycles):
                status["cycles"] += 1
                try:
                    with engine.store.lock():
                        engine.reload_config()
                        before = engine.store.total_attempts()
                        try:
                            result = engine.cycle(local_only, task_limit=max_tasks - status["attempts"])
                        finally:
                            # Count a started attempt even if interruption prevents a cycle result.
                            status["attempts"] += engine.store.total_attempts() - before
                except BusyError:
                    result = {"busy": True, "attempts": 0}
                status["had_errors"] = status["had_errors"] or bool(result.get("errors"))
                status["last_result"] = result
                status["updated_at"] = now()
                engine.store.set_control("worker", status)
                render(engine)
                # Emit outcomes and changes; identical empty/paused polls stay quiet.
                idle = (result.get("blocked"), result.get("busy", False))
                errors = json.dumps(result.get("errors", []), sort_keys=True)
                if (result.get("added") or result.get("runs") or result.get("pr_updates") or result.get('recoveries')
                        or (result.get("errors") and errors != previous_errors)
                        or result.get("attempts") or idle != previous_idle):
                    notify(result)
                previous_idle = idle
                previous_errors = errors
                if status["attempts"] >= max_tasks:
                    status["stop_reason"] = "attempt_limit"
                    break
                if status["cycles"] < max_cycles:
                    sleep(interval)
            status["state"] = "stopped"
            status.setdefault("stop_reason", "cycle_limit")
        except KeyboardInterrupt:
            status.update(state="interrupted", stop_reason="signal")
            raise
        except Exception as exc:
            status.update(state="failed", stop_reason="error", error=str(exc))
            raise
        finally:
            status["updated_at"] = now()
            engine.store.set_control("worker", status)
    render(engine)
    notify({"worker": status["state"], "reason": status["stop_reason"],
            "cycles": status["cycles"], "attempts": status["attempts"]})
    return status
