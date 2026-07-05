"""Unified test runner: delegates to pytest.

Usage:
    python run_all_tests.py            # full run (includes slow integration + eval)
    python run_all_tests.py --quick    # unit + contract only (fast deterministic)
    python run_all_tests.py --smoke    # includes real-LLM smoke tests (needs API key)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable


def main() -> int:
    args = sys.argv[1:]
    quick = "--quick" in args
    smoke = "--smoke" in args

    pytest_args = [
        PYTHON, "-m", "pytest",
        str(BASE_DIR / "tests"),
        "-v", "--tb=short",
    ]

    if quick:
        # Only unit + contract (skip slow integration, eval benchmark sanity, and smoke)
        pytest_args += ["-m", "not slow and not smoke and not eval"]
    elif smoke:
        # Everything including smoke
        pass
    else:
        # Default: skip smoke (needs API key) but run slow integration
        pytest_args += ["-m", "not smoke"]

    print(f"biz-analyst-agent test runner")
    print(f"mode: {'quick' if quick else 'smoke' if smoke else 'full'}")
    print(f"command: {' '.join(pytest_args[2:])}")
    print()

    result = subprocess.run(pytest_args, cwd=str(BASE_DIR))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
