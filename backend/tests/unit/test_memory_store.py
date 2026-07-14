"""Tests for memory store migration idempotency and concurrent writes."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

import memory_store

# Add scripts/ to path for import
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "scripts"


# ================================================================
#  Migration idempotency
# ================================================================

def test_migration_idempotent(tmp_path, monkeypatch):
    """Running migration twice must not change file contents."""
    sys.path.insert(0, str(_SCRIPTS_DIR))
    import migrate_memory as migrate_mod

    mem = tmp_path / "memory"
    mem.mkdir()
    monkeypatch.setattr(migrate_mod, "MEM", mem)
    monkeypatch.setattr(migrate_mod, "OLD", mem / "_old")
    monkeypatch.setattr(migrate_mod, "_MARKER", mem / ".migrated_v3")
    monkeypatch.setattr(migrate_mod, "_OLD_SOURCES", [
        mem / "challenge_policies.json",
        mem / "strategy_cards.json",
        mem / "eval_feedback.json",
        mem / "episodic",
        mem / "industry_rag",
        mem / "meta_reflection",
    ])

    # Create old-layout files
    (mem / "challenge_policies.json").write_text(
        json.dumps({"policies": [{"id": "pol_1", "trigger": "test"}]}),
        encoding="utf-8")
    (mem / "strategy_cards.json").write_text(
        json.dumps({"active": [{"id": "sc_1", "stage": "collect"}]}),
        encoding="utf-8")
    (mem / "eval_feedback.json").write_text(
        json.dumps({"feedbacks": [{"query_key": "x", "timestamp": "t1"}]}),
        encoding="utf-8")
    ep_dir = mem / "episodic" / "runs"
    ep_dir.mkdir(parents=True)
    (ep_dir / "2026-07-01.json").write_text(
        json.dumps({"runs": [{"run_id": "r1", "query": "test"}]}),
        encoding="utf-8")
    rag_dir = mem / "industry_rag"
    rag_dir.mkdir()
    (rag_dir / "playbooks.json").write_text(
        json.dumps({"generic": {"key_dimensions": ["x"]}}),
        encoding="utf-8")
    (mem / "meta_reflection").mkdir()

    # First migration
    result1 = migrate_mod.migrate()
    assert result1 is True

    # Read new files after first migration
    exp1 = (mem / "experience.json").read_text(encoding="utf-8")
    dom1 = (mem / "domain.json").read_text(encoding="utf-8")
    beh1 = (mem / "behavior.json").read_text(encoding="utf-8")

    # Verify content
    exp_data = json.loads(exp1)
    assert len(exp_data["runs"]) == 1
    assert exp_data["runs"][0]["run_id"] == "r1"
    beh_data = json.loads(beh1)
    assert len(beh_data["policies"]) == 1
    assert len(beh_data["strategies"]["active"]) == 1

    # Second migration — must skip
    result2 = migrate_mod.migrate()
    assert result2 is False

    # Files unchanged
    assert (mem / "experience.json").read_text(encoding="utf-8") == exp1
    assert (mem / "domain.json").read_text(encoding="utf-8") == dom1
    assert (mem / "behavior.json").read_text(encoding="utf-8") == beh1


# ================================================================
#  Concurrent writes to behavior.json
# ================================================================

def test_concurrent_policy_update_and_strategy_promote(tmp_path, monkeypatch):
    """Concurrent update_policy_success + promote_strategy_candidates must
    both preserve their changes (no lost updates)."""
    beh_path = tmp_path / "behavior.json"
    monkeypatch.setattr(memory_store, "_BEHAVIOR", beh_path)
    memory_store._cache.clear()

    # Seed with one policy
    seed = {
        "policies": [{
            "id": "pol_concurrent",
            "trigger": "test concurrent",
            "challenge_type": "temporal",
            "times_applied": 0,
            "times_succeeded": 0,
            "historical_success_rate": 0.0,
            "last_updated": "2026-01-01T00:00:00",
        }],
        "strategies": {"active": [], "pending": [], "rejected": []},
    }
    beh_path.write_text(json.dumps(seed), encoding="utf-8")

    barrier = threading.Barrier(2, timeout=5)
    errors = []

    def update_policy():
        try:
            barrier.wait()
            for _ in range(10):
                memory_store.update_policy_success("pol_concurrent", True)
        except Exception as e:
            errors.append(e)

    def promote_strategies():
        try:
            barrier.wait()
            for i in range(10):
                memory_store.promote_strategy_candidates([{
                    "stage": "collect",
                    "trigger": f"concurrent test {i}",
                    "problem": "Testing concurrent writes to behavior.json",
                    "action": {"query_templates": [f"query {i}"]},
                    "success_metric": "Must survive concurrent writes",
                    "confidence": 0.8,
                }], source_run_id=f"concurrent_{i}")
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=update_policy)
    t2 = threading.Thread(target=promote_strategies)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"Concurrent writes raised errors: {errors}"

    # Verify both sets of changes survived
    memory_store._cache.clear()
    final = json.loads(beh_path.read_text(encoding="utf-8"))

    # Policy must have been applied 10 times
    pol = final["policies"][0]
    assert pol["times_applied"] == 10, f"Expected 10 applications, got {pol['times_applied']}"
    assert pol["times_succeeded"] == 10, f"Expected 10 successes, got {pol['times_succeeded']}"

    # All 10 strategies must be present
    active = final["strategies"]["active"]
    assert len(active) == 10, f"Expected 10 active strategies, got {len(active)}"
