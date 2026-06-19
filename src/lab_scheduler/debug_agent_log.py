"""Optional NDJSON debug logging for Auto-Pilot instrumentation."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping


def agent_debug_log(
    *,
    hypothesis_id: str,
    location: str,
    message: str,
    data: Mapping[str, Any] | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
) -> None:
    if os.environ.get("LAB_SCHEDULER_AGENT_DEBUG", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }:
        return

    payload = {
        "sessionId": session_id or os.environ.get("LAB_SCHEDULER_DEBUG_SESSION", "local"),
        "runId": run_id or os.environ.get("LAB_SCHEDULER_DEBUG_RUN", "run"),
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": dict(data or {}),
        "timestamp": int(time.time() * 1000),
    }
    log_path = Path(
        os.environ.get(
            "LAB_SCHEDULER_DEBUG_LOG",
            str(Path.cwd().parent / "debug-local.log"),
        )
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str) + "\n")
