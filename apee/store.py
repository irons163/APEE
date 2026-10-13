"""Durable queue and event history; one local engine mutation at a time."""

import fcntl
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import ApeeError, BusyError


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.db = sqlite3.connect(str(root / "state.sqlite3"), timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, repo TEXT NOT NULL, spec TEXT NOT NULL,
                state TEXT NOT NULL, priority INTEGER NOT NULL,
                created TEXT NOT NULL, updated TEXT NOT NULL, run_id TEXT
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                at TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS controls (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usage_records (
                run_id TEXT PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_state_at ON events(state, at);
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def lock(self, name="engine"):
        if name not in ("engine", "worker"):
            raise ValueError("Unknown lock name")
        with (self.root / f"{name}.lock").open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BusyError("另一個 APEE 執行正在使用此工作區。") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def enqueue(self, task_id, repo, spec, priority):
        stamp = now()
        with self.db:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO tasks VALUES (?, ?, ?, 'queued', ?, ?, ?, NULL)",
                (task_id, repo, json.dumps(spec, ensure_ascii=False), priority, stamp, stamp),
            ).rowcount
            if inserted:
                self._event(task_id, "queued", {"source": spec["source"]})
        return bool(inserted)

    def _event(self, task_id, state, detail):
        self.db.execute(
            "INSERT INTO events(task_id, at, state, detail) VALUES (?, ?, ?, ?)",
            (task_id, now(), state, json.dumps(detail, ensure_ascii=False)),
        )

    def transition(self, task_id, state, detail=None, run=None):
        with self.db:
            self._transition(task_id, state, detail, run)

    def save_run(self, run):
        """Update an observation without inventing another task transition."""
        with self.db:
            self.db.execute("UPDATE runs SET data=? WHERE id=? AND task_id=?",
                            (json.dumps(run, ensure_ascii=False), run["id"], run["task_id"]))

    def _transition(self, task_id, state, detail=None, run=None):
        if run:
            self.db.execute(
                "INSERT OR REPLACE INTO runs VALUES (?, ?, ?)",
                (run["id"], task_id, json.dumps(run, ensure_ascii=False)),
            )
            self.db.execute("UPDATE tasks SET run_id=? WHERE id=?", (run["id"], task_id))
        self.db.execute("UPDATE tasks SET state=?, updated=? WHERE id=?", (state, now(), task_id))
        self._event(task_id, state, detail or {})

    def control(self, key):
        row = self.db.execute("SELECT value FROM controls WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _set_control(self, key, value):
        if value is None:
            self.db.execute("DELETE FROM controls WHERE key=?", (key,))
        else:
            self.db.execute("INSERT OR REPLACE INTO controls VALUES (?, ?)",
                            (key, json.dumps(value, ensure_ascii=False)))

    def set_control(self, key, value):
        with self.db:
            self._set_control(key, value)

    def defer(self, task_id, run, reason):
        # Save the provider hold and task state atomically, including across restarts.
        with self.db:
            self._set_control("codex_hold", dict(reason, at=now(), task=task_id))
            self._transition(task_id, "waiting", reason, run)

    def resume_codex(self):
        # Caller owns engine.lock, so a running failure cannot be accidentally cleared.
        with self.db:
            self._set_control("codex_hold", None)
            tasks = self.db.execute("SELECT id FROM tasks WHERE state='waiting'").fetchall()
            for task in tasks:
                self._transition(task[0], "queued", {"requested_by": "user", "reason": "resume_codex"})
        return [task[0] for task in tasks]

    def attempts_today(self):
        day = now()[:10]
        count = self.db.execute("SELECT COUNT(*) FROM events WHERE state='preparing' AND at LIKE ?",
                                (day + "T%",)).fetchone()[0]
        return {"date": day, "used": count}

    def total_attempts(self):
        return self.db.execute("SELECT COUNT(*) FROM events WHERE state='preparing'").fetchone()[0]

    def worker_active(self):
        try:
            with self.lock("worker"):
                return False
        except BusyError:
            return True

    def engine_active(self):
        try:
            with self.lock():
                return False
        except BusyError:
            return True

    def task(self, task_id):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ApeeError(f"找不到任務：{task_id}")
        result = dict(row)
        result["spec"] = json.loads(result["spec"])
        return result

    def tasks(self):
        return [self.task(row[0]) for row in self.db.execute(
            "SELECT id FROM tasks ORDER BY priority, created, id"
        ).fetchall()]

    def run(self, task_id):
        task = self.task(task_id)
        row = self.db.execute("SELECT data FROM runs WHERE id=?", (task["run_id"],)).fetchone()
        if not row:
            raise ApeeError("此任務還沒有執行記錄。")
        return json.loads(row[0])

    def runs(self, task_id=None):
        if task_id is None:
            rows = self.db.execute("SELECT data FROM runs ORDER BY id")
        else:
            rows = self.db.execute("SELECT data FROM runs WHERE task_id=? ORDER BY id", (task_id,))
        return [json.loads(row[0]) for row in rows]

    def usage_records(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM usage_records ORDER BY run_id")]

    def save_usage(self, record):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO usage_records VALUES (?, ?)",
                            (record["run_id"], json.dumps(record, ensure_ascii=False)))
