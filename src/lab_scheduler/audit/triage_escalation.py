from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from lab_scheduler.workers.logic_worker import LogicWorkerOutput, LogicWorkerStatus, TriageEntry

TRIAGE_ESCALATION_PREFIX = "Triage_Escalation"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def triage_escalation_path(project_root: Path, report_date: Optional[date] = None) -> Path:
    stamp = (report_date or date.today()).isoformat()
    return project_root / "exports" / f"{TRIAGE_ESCALATION_PREFIX}_{stamp}.json"


def write_triage_escalation_report(
    project_root: Path,
    logic_output: LogicWorkerOutput,
    *,
    period_start: date,
    period_end: date,
    report_date: Optional[date] = None,
) -> Path:
    """Persist orchestrator triage handoff JSON for human escalation."""

    path = triage_escalation_path(project_root, report_date=report_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at_utc": _utc_now_iso(),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "status": logic_output.status.value,
        "triage_count": len(logic_output.triage_list),
        "triage_list": [entry.to_dict() for entry in logic_output.triage_list],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_latest_triage_escalation(project_root: Path) -> Optional[dict]:
    exports_dir = project_root / "exports"
    if not exports_dir.is_dir():
        return None
    candidates = sorted(
        exports_dir.glob(f"{TRIAGE_ESCALATION_PREFIX}_*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    return json.loads(candidates[0].read_text(encoding="utf-8"))


def relative_export_path(project_root: Path, absolute_path: Path) -> str:
    try:
        return absolute_path.relative_to(project_root).as_posix()
    except ValueError:
        return absolute_path.as_posix()
