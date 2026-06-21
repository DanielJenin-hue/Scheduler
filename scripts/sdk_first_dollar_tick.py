#!/usr/bin/env python3
"""
FINISH_APP SDK tick — one local Cursor agent run per invocation.

Runs the RSI gate, reports PASS/FAIL, and summarizes human blockers from
docs/FINISH_APP_ITERATIONS.md. Intended for cron, CI, or manual revenue loops.

Usage:
    set CURSOR_API_KEY=cursor_...
    python scripts/sdk_first_dollar_tick.py

Requires: pip install cursor-sdk  (see docs/CURSOR_SDK.md)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TICK_PROMPT = """\
FINISH_APP SDK tick for lab_staffing_scheduler ($2,000 CAD MRR north star).

1. Run `python scripts/rotation_rsi_gate.py` from the repo root and capture output.
2. Report RSI gate status as PASS or FAIL (include violation counts if FAIL).
3. Read `docs/FINISH_APP_ITERATIONS.md` and summarize the current FINISH_APP human blockers
   (deploy, outbound mailtos, publish bundle, Stripe, inbox env, RSI/compliance gates).
4. End with a short "next human action" recommendation (one sentence).

Do not send email, deploy, or modify production config. Report only.
"""


def _require_api_key() -> str:
    key = os.environ.get("CURSOR_API_KEY", "").strip()
    if not key:
        print(
            "CURSOR_API_KEY is not set. Create a key at "
            "https://cursor.com/dashboard/integrations",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one local Cursor SDK tick (RSI gate + FINISH_APP blocker summary)."
    )
    parser.add_argument(
        "--model",
        default="composer-2.5",
        help="Cursor model id (default: composer-2.5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the prompt and exit without calling the SDK",
    )
    args = parser.parse_args()

    if args.dry_run:
        print(TICK_PROMPT)
        return 0

    try:
        from cursor_sdk import Agent, CursorAgentError, LocalAgentOptions
    except ImportError:
        print(
            "cursor-sdk is not installed. Run: pip install cursor-sdk",
            file=sys.stderr,
        )
        return 1

    api_key = _require_api_key()

    try:
        with Agent.create(
            model=args.model,
            api_key=api_key,
            local=LocalAgentOptions(cwd=str(ROOT)),
        ) as agent:
            run = agent.send(TICK_PROMPT)
            print(f"agent_id={agent.agent_id} run_id={run.id}", file=sys.stderr)

            for message in run.messages():
                if message.type == "assistant":
                    for block in message.message.content:
                        if block.type == "text":
                            print(block.text, end="")

            result = run.wait()
    except CursorAgentError as err:
        print(
            f"startup failed: {err.message}, retryable={err.is_retryable}",
            file=sys.stderr,
        )
        return 1

    if result.status == "error":
        print(f"run failed: {result.id}", file=sys.stderr)
        return 2

    if result.status != "finished":
        print(f"run ended with status={result.status!r}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
