"""Contract tests: store run lifecycle."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import store
from schemas import AnalysisRun
from store import list_runs, mark_stale_runs_interrupted, save_run


class TestStaleRunInterruption:
    def test_marks_stale_run_interrupted(self):
        old_path = store._DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store._DB_PATH = Path(tmp) / "runs.db"
                run = AnalysisRun(query="stale run")
                run.status = "running"
                run.created_at = "2026-06-01T10:00:00"
                run.updated_at = "2026-06-01T10:00:00"
                save_run(run)

                conn = store._conn()
                try:
                    conn.execute(
                        "UPDATE runs SET created_at = ?, updated_at = ? WHERE id = ?",
                        ("2026-06-01T10:00:00", "2026-06-01T10:00:00", run.id),
                    )
                    conn.commit()
                finally:
                    conn.close()

                changed = mark_stale_runs_interrupted(timeout_minutes=1)
                rows = list_runs()
                assert changed == 1
                assert rows[0]["status"] == "interrupted"
        finally:
            store._DB_PATH = old_path
