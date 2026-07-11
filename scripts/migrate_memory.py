"""
Migrate memory/ from 6-store layout to 3-store layout.

Old:
  memory/challenge_policies.json
  memory/strategy_cards.json
  memory/eval_feedback.json
  memory/episodic/runs/*.json
  memory/industry_rag/playbooks.json
  memory/meta_reflection/reflections.json

New:
  memory/experience.json   (episodic runs + eval_feedback)
  memory/domain.json       (industry_rag playbooks)
  memory/behavior.json     (challenge_policies + strategy_cards)

Idempotent: safe to run multiple times. Uses a marker file to skip
re-migration when old sources are already gone. Never overwrites
new files with empty data.
"""
import json
import shutil
from pathlib import Path

MEM = Path(__file__).resolve().parent.parent / "memory"
OLD = MEM / "_old"
_MARKER = MEM / ".migrated_v3"

# Old source files/dirs that would be consumed
_OLD_SOURCES = [
    MEM / "challenge_policies.json",
    MEM / "strategy_cards.json",
    MEM / "eval_feedback.json",
    MEM / "episodic",
    MEM / "industry_rag",
    MEM / "meta_reflection",
]


def _has_old_sources() -> bool:
    """Check if any old-layout source still exists."""
    return any(p.exists() for p in _OLD_SOURCES)


def migrate() -> bool:
    """Run migration. Returns True if work was done, False if skipped.

    Idempotent guarantees:
    - If marker exists and no old sources remain, skip entirely.
    - If new files already exist, merge into them (don't overwrite).
    - Marker is written only after successful completion.
    """
    # --- Guard: already migrated ---
    if _MARKER.exists() and not _has_old_sources():
        print("Migration already completed (marker found, no old sources). Skipping.")
        return False

    MEM.mkdir(parents=True, exist_ok=True)
    OLD.mkdir(parents=True, exist_ok=True)

    # --- 1. Experience: episodic runs + eval_feedback ---
    exp_path = MEM / "experience.json"
    # Load existing new file if present (merge, don't overwrite)
    if exp_path.exists():
        try:
            experience = json.loads(exp_path.read_text(encoding="utf-8"))
            if not isinstance(experience, dict):
                experience = {}
        except (json.JSONDecodeError, OSError):
            experience = {}
    else:
        experience = {}
    experience.setdefault("runs", [])
    experience.setdefault("eval_feedbacks", [])

    existing_run_ids = {r.get("run_id") for r in experience["runs"] if isinstance(r, dict)}

    episodic_dir = MEM / "episodic" / "runs"
    if episodic_dir.exists():
        for f in sorted(episodic_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                runs = data.get("runs", []) if isinstance(data, dict) else []
                for r in runs:
                    if isinstance(r, dict) and r.get("run_id") not in existing_run_ids:
                        experience["runs"].append(r)
                        existing_run_ids.add(r.get("run_id"))
            except Exception as e:
                print(f"  WARN: skip {f.name}: {e}")

    eval_fb = MEM / "eval_feedback.json"
    if eval_fb.exists():
        try:
            data = json.loads(eval_fb.read_text(encoding="utf-8"))
            fbs = data.get("feedbacks", []) if isinstance(data, dict) else []
            # Merge: append only feedbacks not already present (by timestamp+query_key)
            existing_keys = {
                (f.get("timestamp", ""), f.get("query_key", ""))
                for f in experience["eval_feedbacks"] if isinstance(f, dict)
            }
            for fb in fbs:
                if isinstance(fb, dict):
                    key = (fb.get("timestamp", ""), fb.get("query_key", ""))
                    if key not in existing_keys:
                        experience["eval_feedbacks"].append(fb)
                        existing_keys.add(key)
        except Exception as e:
            print(f"  WARN: skip eval_feedback.json: {e}")

    # Keep only last 100 runs
    if len(experience["runs"]) > 100:
        experience["runs"] = experience["runs"][-100:]

    exp_path.write_text(json.dumps(experience, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  experience.json: {len(experience['runs'])} runs, {len(experience['eval_feedbacks'])} evals")

    # --- 2. Domain: industry_rag playbooks ---
    domain_path = MEM / "domain.json"
    playbooks = MEM / "industry_rag" / "playbooks.json"
    if playbooks.exists():
        # Only copy if domain.json doesn't exist or is empty
        if not domain_path.exists() or domain_path.stat().st_size < 10:
            shutil.copy2(playbooks, domain_path)
            print(f"  domain.json: copied from industry_rag/playbooks.json")
        else:
            print(f"  domain.json: already exists, kept as-is")
    elif not domain_path.exists():
        domain_path.write_text("{}", encoding="utf-8")
        print(f"  domain.json: created empty (no playbooks found)")
    else:
        print(f"  domain.json: already exists, no old source to merge")

    # --- 3. Behavior: challenge_policies + strategy_cards ---
    beh_path = MEM / "behavior.json"
    if beh_path.exists():
        try:
            behavior = json.loads(beh_path.read_text(encoding="utf-8"))
            if not isinstance(behavior, dict):
                behavior = {}
        except (json.JSONDecodeError, OSError):
            behavior = {}
    else:
        behavior = {}
    behavior.setdefault("policies", [])
    behavior.setdefault("strategies", {"active": [], "pending": [], "rejected": []})

    existing_policy_ids = {p.get("id") for p in behavior["policies"] if isinstance(p, dict)}

    cp = MEM / "challenge_policies.json"
    if cp.exists():
        try:
            data = json.loads(cp.read_text(encoding="utf-8"))
            policies = data.get("policies", []) if isinstance(data, dict) else []
            for p in policies:
                if isinstance(p, dict) and p.get("id") not in existing_policy_ids:
                    behavior["policies"].append(p)
                    existing_policy_ids.add(p.get("id"))
        except Exception as e:
            print(f"  WARN: skip challenge_policies.json: {e}")

    sc = MEM / "strategy_cards.json"
    if sc.exists():
        try:
            data = json.loads(sc.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                existing_strat_ids = {
                    s.get("id")
                    for bucket in ("active", "pending", "rejected")
                    for s in behavior["strategies"].get(bucket, [])
                    if isinstance(s, dict)
                }
                for bucket in ("active", "pending", "rejected"):
                    for s in data.get(bucket, []):
                        if isinstance(s, dict) and s.get("id") not in existing_strat_ids:
                            behavior["strategies"].setdefault(bucket, []).append(s)
                            existing_strat_ids.add(s.get("id"))
        except Exception as e:
            print(f"  WARN: skip strategy_cards.json: {e}")

    beh_path.write_text(json.dumps(behavior, ensure_ascii=False, indent=2), encoding="utf-8")
    n_pol = len(behavior["policies"])
    n_strat = len(behavior["strategies"].get("active", []))
    print(f"  behavior.json: {n_pol} policies, {n_strat} active strategies")

    # --- 4. Move old files to _old/ ---
    for item in _OLD_SOURCES:
        if item.exists():
            dst = OLD / item.name
            if dst.exists():
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            shutil.move(str(item), str(dst))
            print(f"  Moved {item.name} -> _old/{item.name}")

    # --- 5. Write marker ---
    _MARKER.write_text("migrated", encoding="utf-8")

    print("\nMigration complete!")
    print(f"New files: experience.json, domain.json, behavior.json")
    print(f"Old files backed up to: memory/_old/")
    return True


if __name__ == "__main__":
    migrate()
