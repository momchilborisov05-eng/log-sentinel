"""SQLite storage for metric points, log lines and mined log templates (stdlib ``sqlite3``, WAL mode)."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path

from .logmine import Template

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    id     INTEGER PRIMARY KEY,
    ts     REAL NOT NULL,
    metric TEXT NOT NULL,
    value  REAL NOT NULL,
    host   TEXT NOT NULL DEFAULT 'api-1'
);
CREATE INDEX IF NOT EXISTS ix_metrics_metric_ts ON metrics(metric, ts);

CREATE TABLE IF NOT EXISTS templates (
    id         INTEGER PRIMARY KEY,
    template   TEXT NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY,
    ts          REAL NOT NULL,
    level       TEXT NOT NULL,
    service     TEXT NOT NULL,
    message     TEXT NOT NULL,
    template_id INTEGER REFERENCES templates(id)
);
CREATE INDEX IF NOT EXISTS ix_logs_ts ON logs(ts);
CREATE INDEX IF NOT EXISTS ix_logs_template_ts ON logs(template_id, ts);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    """Thin data-access layer. One connection guarded by a lock: FastAPI runs sync endpoints in a thread pool."""

    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            if path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ---------------------------------------------------------------- writes

    def insert_metrics(self, rows: Iterable[tuple[float, str, float, str]]) -> int:
        rows = list(rows)
        with self.lock, self.conn:
            self.conn.executemany("INSERT INTO metrics(ts, metric, value, host) VALUES (?, ?, ?, ?)", rows)
        return len(rows)

    def insert_logs(self, rows: Iterable[tuple[float, str, str, str, int]]) -> int:
        rows = list(rows)
        with self.lock, self.conn:
            self.conn.executemany(
                "INSERT INTO logs(ts, level, service, message, template_id) VALUES (?, ?, ?, ?, ?)", rows)
        return len(rows)

    def upsert_templates(self, templates: Iterable[Template]) -> None:
        with self.lock, self.conn:
            self.conn.executemany(
                "INSERT INTO templates(id, template, count, first_seen, last_seen) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET template=excluded.template, count=excluded.count, "
                "first_seen=excluded.first_seen, last_seen=excluded.last_seen",
                [(t.id, t.text, t.count, t.first_seen, t.last_seen) for t in templates],
            )

    def clear(self) -> None:
        with self.lock, self.conn:
            for table in ("logs", "templates", "metrics", "meta"):
                self.conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names

    def set_meta(self, key: str, value: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, value))

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    # ---------------------------------------------------------------- reads

    def metric_names(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.conn.execute("SELECT DISTINCT metric FROM metrics ORDER BY metric")]

    def series(self, metric: str, since: float | None = None, until: float | None = None) -> tuple[list[float], list[float]]:
        """Points of one metric ordered by time. Several hosts reporting the same metric are averaged per timestamp."""
        sql = "SELECT ts, AVG(value) FROM metrics WHERE metric = ?"
        args: list = [metric]
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since)
        if until is not None:
            sql += " AND ts <= ?"
            args.append(until)
        sql += " GROUP BY ts ORDER BY ts"
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [r[0] for r in rows], [r[1] for r in rows]

    def counts(self) -> dict[str, int]:
        with self.lock:
            c = self.conn.execute(
                "SELECT (SELECT COUNT(*) FROM metrics), (SELECT COUNT(*) FROM logs), (SELECT COUNT(*) FROM templates)"
            ).fetchone()
        return {"metric_points": c[0], "log_lines": c[1], "templates": c[2]}

    def time_range(self) -> tuple[float | None, float | None]:
        with self.lock:
            r = self.conn.execute(
                "SELECT MIN(t), MAX(t) FROM (SELECT MIN(ts) t FROM metrics UNION ALL SELECT MAX(ts) FROM metrics "
                "UNION ALL SELECT MIN(ts) FROM logs UNION ALL SELECT MAX(ts) FROM logs)").fetchone()
        return r[0], r[1]

    def data_version(self) -> str:
        """Changes whenever rows are added or removed; used to cache analysis results."""
        with self.lock:
            r = self.conn.execute(
                "SELECT (SELECT IFNULL(MAX(id),0) FROM metrics), (SELECT COUNT(*) FROM metrics), "
                "(SELECT IFNULL(MAX(id),0) FROM logs), (SELECT COUNT(*) FROM logs)").fetchone()
        return ":".join(str(x) for x in r)

    def templates(self) -> list[Template]:
        with self.lock:
            rows = self.conn.execute("SELECT id, template, count, first_seen, last_seen FROM templates ORDER BY id").fetchall()
        return [Template(r["id"], r["template"].split(" "), r["count"], r["first_seen"], r["last_seen"]) for r in rows]

    def template_occurrences(self, since: float | None = None) -> dict[int, list[float]]:
        sql = "SELECT template_id, ts FROM logs"
        args: list = []
        if since is not None:
            sql += " WHERE ts >= ?"
            args.append(since)
        out: dict[int, list[float]] = {}
        with self.lock:
            for tid, ts in self.conn.execute(sql, args):
                out.setdefault(tid, []).append(ts)
        return out

    def recent_logs(self, limit: int = 50, template_id: int | None = None, level: str | None = None) -> list[dict]:
        sql = "SELECT id, ts, level, service, message, template_id FROM logs"
        where, args = [], []
        if template_id is not None:
            where.append("template_id = ?")
            args.append(template_id)
        if level:
            where.append("level = ?")
            args.append(level)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args)]

    def level_counts(self, since: float) -> dict[str, int]:
        with self.lock:
            return {r[0]: r[1] for r in self.conn.execute(
                "SELECT level, COUNT(*) FROM logs WHERE ts >= ? GROUP BY level", (since,))}
