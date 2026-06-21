# Lab Staffing Scheduler (backend)

Python backend utilities for medical laboratory staffing schedules, rotations, and compliance logic.

## Key rule (non-negotiable)

The scheduling logic engine defines the **standard work week as starting on Monday** (regardless of when a cycle starts).

## Dev quickstart

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
set LAB_ALLOW_DEMO_ACCOUNTS=1
pytest
```

Bundled demo logins (`northstar_admin`, `southbridge_admin`) are **dev-only**. Enable with
`LAB_ALLOW_DEMO_ACCOUNTS=1` and optional `LAB_DEMO_NORTHSTAR_PASSWORD` /
`LAB_DEMO_SOUTHBRIDGE_PASSWORD` overrides. Never enable on production hosts
(`LAB_SCHEDULER_ENV=production`).

Optional Cursor SDK tick (RSI gate + FINISH_APP blocker summary): see [docs/CURSOR_SDK.md](docs/CURSOR_SDK.md).

