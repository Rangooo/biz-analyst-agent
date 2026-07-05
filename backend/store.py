"""SQLite 持久化 —— 分析任务状态外置、可审计。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from schemas import AnalysisRun

_DB_PATH = Path(__file__).parent / "data" / "runs.db"
ACTIVE_STATUSES = {"pending", "running"}


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            query TEXT,
            status TEXT,
            data TEXT,
            created_at TEXT,
            updated_at TEXT
        )"""
    )
    return conn


def save_run(run: AnalysisRun) -> None:
    run.updated_at = datetime.now().isoformat(timespec="seconds")
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO runs (id, query, status, data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run.id, run.query, run.status, run.model_dump_json(),
             run.created_at, run.updated_at),
        )
        conn.commit()
    finally:
        conn.close()


def load_run(run_id: str) -> Optional[AnalysisRun]:
    conn = _conn()
    try:
        row = conn.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        return AnalysisRun.model_validate_json(row[0])
    finally:
        conn.close()


def _parse_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _mark_interrupted(data: str, now_iso: str, reason: str) -> str:
    try:
        payload = json.loads(data or "{}")
    except json.JSONDecodeError:
        payload = {}
    payload["status"] = "interrupted"
    payload["updated_at"] = now_iso
    payload["error"] = reason
    return json.dumps(payload, ensure_ascii=False)


def mark_stale_runs_interrupted(timeout_minutes: int = 90) -> int:
    """Close out persisted active runs that survived a process restart.

    The in-memory scheduler is the source of truth for active work. If a row is
    still pending/running in SQLite and has not been updated for the timeout
    window, it is almost certainly an abandoned local run from a previous
    session. Marking it interrupted keeps the history page honest.
    """
    now = datetime.now()
    cutoff = now - timedelta(minutes=timeout_minutes)
    now_iso = now.isoformat(timespec="seconds")
    reason = f"Run was marked interrupted after {timeout_minutes} minutes without updates."
    conn = _conn()
    changed = 0
    try:
        rows = conn.execute(
            "SELECT id, status, data, created_at, updated_at FROM runs WHERE status IN (?, ?)",
            ("pending", "running"),
        ).fetchall()
        for run_id, status, data, created_at, updated_at in rows:
            last_seen = _parse_dt(updated_at) or _parse_dt(created_at)
            if last_seen and last_seen > cutoff:
                continue
            conn.execute(
                "UPDATE runs SET status = ?, data = ?, updated_at = ? WHERE id = ?",
                ("interrupted", _mark_interrupted(data, now_iso, reason), now_iso, run_id),
            )
            changed += 1
        if changed:
            conn.commit()
    finally:
        conn.close()
    return changed


def list_runs(limit: int = 50) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, query, status, created_at FROM runs "
            "ORDER BY created_at DESC LIMIT ?", (limit,),
        ).fetchall()
        return [
            {"id": r[0], "query": r[1], "status": r[2], "created_at": r[3]}
            for r in rows
        ]
    finally:
        conn.close()
