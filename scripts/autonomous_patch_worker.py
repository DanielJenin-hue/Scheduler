#!/usr/bin/env python3
"""
Autonomous DevOps Orchestrator — background patch worker daemon.

Polls `sys_sentry_logs` every minute, requests a surgical LLM patch for the
oldest unresolved incident, validates with pytest, and queues passing patches
for human review (HITL). Production files are never overwritten directly.

Usage:
    python scripts/autonomous_patch_worker.py
    python scripts/autonomous_patch_worker.py --once

Environment:
    PATCH_LLM_API_KEY           LLM provider API key (or OPENAI_API_KEY)
    PATCH_LLM_API_BASE          API base URL (default: https://api.openai.com/v1)
    PATCH_LLM_MODEL             Model name (default: gpt-4o-mini)
    PATCH_WORKER_POLL_SECONDS   Poll interval in seconds (default: 60)
    PATCH_WORKER_STABLE_BRANCH  Stable branch to restore after isolated commits
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lab_scheduler.telemetry.patch_worker import (  # noqa: E402
    PatchWorkerConfig,
    run_patch_worker_loop,
)

DEFAULT_DB_PATH = ROOT / "demo.sqlite3"


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous Sentry patch worker daemon")
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="SQLite database path (default: demo.sqlite3)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll cycle and exit",
    )
    args = parser.parse_args()

    config = PatchWorkerConfig.from_env(project_root=ROOT, db_path=args.db.resolve())
    run_patch_worker_loop(config, run_once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
