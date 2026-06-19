from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from lab_scheduler.audit.triage_escalation import relative_export_path
from lab_scheduler.errors.schedule_error import ScheduleError
from lab_scheduler.paths import resolve_project_path
from lab_scheduler.scheduling.agency_fulfillment import (
    resolve_employee_id_for_slot,
    shift_template_id_for_code,
)
from lab_scheduler.scheduling.schedule_export import load_triage_escalation_payload

logger = logging.getLogger(__name__)

AGENCY_REQUEST_PREFIX = "Agency_Request"
AGENCY_EMAIL_DRAFT_PREFIX = "Agency_Email_Draft"
DEFAULT_VENDOR_ID = "locum-north-manitoba"
DEFAULT_HOURS_PER_SHIFT = 8.0
QUAL_PATTERN = re.compile(r"\b(MLT|MLA)\b", re.IGNORECASE)

STATUS_DRAFT_PENDING_APPROVAL = "DRAFT_PENDING_APPROVAL"
STATUS_SENT = "SENT"
STATUS_FULFILLED = "FULFILLED"
STATUS_CLOSED_UNFILLED = "CLOSED_UNFILLED"
STATUS_PARTIALLY_FULFILLED = "PARTIALLY_FULFILLED"

LINE_OPEN = "OPEN"
LINE_PLACEHOLDER_CREATED = "PLACEHOLDER_CREATED"
LINE_FULFILLED = "FULFILLED"
LINE_CLOSED_UNFILLED = "CLOSED_UNFILLED"

DEFAULT_PLACEHOLDER_LABEL = "Agency - TBD"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def agency_request_path(project_root: Path, report_date: Optional[date] = None) -> Path:
    stamp = (report_date or date.today()).isoformat()
    return resolve_project_path(project_root, f"exports/{AGENCY_REQUEST_PREFIX}_{stamp}.json")


def agency_email_draft_path(project_root: Path, report_date: Optional[date] = None) -> Path:
    stamp = (report_date or date.today()).isoformat()
    return resolve_project_path(project_root, f"exports/{AGENCY_EMAIL_DRAFT_PREFIX}_{stamp}.txt")


@dataclass(frozen=True, slots=True)
class AgencyHandoffResult:
    request_path: Optional[Path]
    email_draft_path: Optional[Path]
    request_relative_path: Optional[str]
    email_draft_relative_path: Optional[str]
    line_item_count: int
    escalated_slot_count: int
    status: str


def _parse_iso_date(value: object) -> Optional[date]:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _required_qual_from_entry(entry: Mapping[str, object]) -> str:
    slot_id = str(entry.get("slot_id") or "")
    if "|qual=" in slot_id:
        qual = slot_id.rsplit("|qual=", 1)[-1].strip().upper()
        if qual:
            return qual
    slot = str(entry.get("slot") or "")
    match = QUAL_PATTERN.search(slot)
    if match:
        return match.group(1).upper()
    return "ANY"


def _shift_code_from_entry(entry: Mapping[str, object]) -> str:
    return str(entry.get("shift_code") or "UNKNOWN").strip().upper() or "UNKNOWN"


def _slot_label(entry: Mapping[str, object]) -> str:
    return str(entry.get("slot") or entry.get("slot_id") or "").strip()


def line_item_id(item: Mapping[str, object]) -> str:
    explicit = str(item.get("line_item_id") or "").strip()
    if explicit:
        return explicit
    return f"{item.get('date')}|{item.get('shift_code')}|{item.get('required_qual')}"


def _new_line_item(
    *,
    day: date,
    shift_code: str,
    required_qual: str,
    deficit_hours: float,
) -> Dict[str, object]:
    return {
        "line_item_id": f"{day.isoformat()}|{shift_code}|{required_qual}",
        "date": day.isoformat(),
        "shift_code": shift_code,
        "required_qual": required_qual,
        "headcount": 0,
        "hours_per_shift": deficit_hours,
        "slots": [],
        "blocked_by": ScheduleError.ERR_IMPOSSIBLE_COVERAGE.value,
        "fulfillment_status": LINE_OPEN,
        "fulfillment_notes": "",
        "placements": [],
    }


def _ensure_line_item_metadata(payload: dict) -> None:
    line_items = payload.get("line_items") or []
    for item in line_items:
        if not isinstance(item, dict):
            continue
        item.setdefault("line_item_id", line_item_id(item))
        item.setdefault("fulfillment_status", LINE_OPEN)
        item.setdefault("fulfillment_notes", "")
        item.setdefault("placements", [])


def _save_agency_request(abs_path: Path, payload: dict) -> dict:
    abs_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def _load_and_prepare_request(project_root: Path, request_path: Path | str) -> tuple[Path, dict]:
    abs_path = resolve_project_path(project_root, request_path)
    payload = load_agency_request(abs_path)
    _ensure_line_item_metadata(payload)
    return abs_path, payload


def _sync_request_status(payload: dict) -> None:
    line_items = payload.get("line_items") or []
    if not line_items:
        return
    statuses = {str(item.get("fulfillment_status", LINE_OPEN)) for item in line_items}
    if statuses == {LINE_CLOSED_UNFILLED}:
        payload["status"] = STATUS_CLOSED_UNFILLED
        return
    if LINE_FULFILLED in statuses and statuses <= {LINE_FULFILLED, LINE_CLOSED_UNFILLED}:
        payload["status"] = STATUS_FULFILLED
        return
    if LINE_PLACEHOLDER_CREATED in statuses or LINE_FULFILLED in statuses:
        if payload.get("status") not in {STATUS_FULFILLED, STATUS_CLOSED_UNFILLED}:
            payload["status"] = STATUS_PARTIALLY_FULFILLED


def _filter_impossible_coverage_rows(
    triage_list: Sequence[Mapping[str, object]],
) -> List[Mapping[str, object]]:
    blocked = ScheduleError.ERR_IMPOSSIBLE_COVERAGE.value
    return [
        entry
        for entry in triage_list
        if str(entry.get("blocked_by") or entry.get("error_code") or "").strip() == blocked
    ]


def _group_escalation_rows(
    rows: Sequence[Mapping[str, object]],
) -> List[Dict[str, object]]:
    grouped: Dict[Tuple[str, str, str], Dict[str, object]] = {}
    for entry in rows:
        day = _parse_iso_date(entry.get("date") or entry.get("assignment_date"))
        if day is None:
            logger.warning("Agency worker skipping triage row with invalid date: %s", entry)
            continue
        shift_code = _shift_code_from_entry(entry)
        required_qual = _required_qual_from_entry(entry)
        key = (day.isoformat(), shift_code, required_qual)
        line_item = grouped.get(key)
        if line_item is None:
            line_item = _new_line_item(
                day=day,
                shift_code=shift_code,
                required_qual=required_qual,
                deficit_hours=float(entry.get("deficit_hours") or DEFAULT_HOURS_PER_SHIFT),
            )
            grouped[key] = line_item
        slot = _slot_label(entry)
        slots = line_item["slots"]
        assert isinstance(slots, list)
        if slot and slot not in slots:
            slots.append(slot)
        line_item["headcount"] = len(slots)
        deficit = float(entry.get("deficit_hours") or DEFAULT_HOURS_PER_SHIFT)
        if deficit > float(line_item["hours_per_shift"]):
            line_item["hours_per_shift"] = deficit

    return sorted(
        grouped.values(),
        key=lambda item: (str(item["date"]), str(item["shift_code"]), str(item["required_qual"])),
    )


def _build_email_draft(
    *,
    facility_name: str,
    period_start: date,
    period_end: date,
    line_items: Sequence[Mapping[str, object]],
    request_id: str,
) -> str:
    lines = [
        f"Subject: Locum Tenens Request — {facility_name} — {period_start.isoformat()} to {period_end.isoformat()}",
        "",
        "Dear Locum Coordination Team,",
        "",
        (
            f"We are requesting locum coverage for unfilled structural shifts at "
            f"{facility_name} for the schedule block {period_start.isoformat()} through "
            f"{period_end.isoformat()}."
        ),
        "",
        "The following shifts could not be staffed under internal capacity and statutory constraints:",
        "",
    ]
    for item in line_items:
        slots = item.get("slots") or []
        slot_summary = ", ".join(str(slot) for slot in slots) if slots else "—"
        lines.append(
            f"- {item['date']} · {item['shift_code']} · {item['required_qual']} · "
            f"{item['headcount']} shift(s) · {item['hours_per_shift']}h each"
        )
        lines.append(f"  Lines: {slot_summary}")
    lines.extend(
        [
            "",
            f"Reference ID: {request_id}",
            "",
            "Please confirm availability, rate, and credentialing timeline at your earliest convenience.",
            "",
            "Thank you,",
            f"{facility_name} Scheduling Office",
        ]
    )
    return "\n".join(lines) + "\n"


def run_agency_worker(
    project_root: Path,
    triage_path: Path,
    *,
    report_date: Optional[date] = None,
    facility_name: str = "Northstar Medical Laboratory",
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    tenant_id: Optional[str] = None,
    schedule_period_id: Optional[str] = None,
) -> AgencyHandoffResult:
    """
    Build vendor procurement artifacts from triage escalation JSON.

    Dumb renderer only — no generation or constraint evaluation.
    """

    root = project_root.resolve()
    triage_file = triage_path if triage_path.is_absolute() else resolve_project_path(root, triage_path)
    if not triage_file.is_file():
        logger.error("Agency worker triage file missing on disk: %s", triage_file)
        return AgencyHandoffResult(
            request_path=None,
            email_draft_path=None,
            request_relative_path=None,
            email_draft_relative_path=None,
            line_item_count=0,
            escalated_slot_count=0,
            status="SKIPPED_MISSING_TRIAGE",
        )

    payload = load_triage_escalation_payload(triage_file)
    triage_list = payload.get("triage_list") or []
    impossible_rows = _filter_impossible_coverage_rows(triage_list)
    line_items = _group_escalation_rows(impossible_rows)

    if not line_items:
        logger.info(
            "Agency worker found no ERR_IMPOSSIBLE_COVERAGE rows in triage file: %s",
            triage_file,
        )
        return AgencyHandoffResult(
            request_path=None,
            email_draft_path=None,
            request_relative_path=None,
            email_draft_relative_path=None,
            line_item_count=0,
            escalated_slot_count=0,
            status="SKIPPED_NO_ESCALATIONS",
        )

    block_start = period_start or _parse_iso_date(payload.get("period_start")) or date.today()
    block_end = period_end or _parse_iso_date(payload.get("period_end")) or block_start
    stamp = report_date or date.today()
    request_id = f"agency-{stamp.isoformat()}-{schedule_period_id or 'schedule-block'}"

    request_abs = agency_request_path(root, report_date=stamp)
    draft_abs = agency_email_draft_path(root, report_date=stamp)
    request_abs.parent.mkdir(parents=True, exist_ok=True)

    draft_subject = (
        f"Locum Request — {facility_name} — "
        f"{sum(int(item['headcount']) for item in line_items)} shift(s)"
    )
    request_payload = {
        "request_id": request_id,
        "source_triage_path": relative_export_path(root, triage_file),
        "facility_name": facility_name,
        "period_start": block_start.isoformat(),
        "period_end": block_end.isoformat(),
        "tenant_id": tenant_id,
        "schedule_period_id": schedule_period_id,
        "generated_at_utc": _utc_now_iso(),
        "status": STATUS_DRAFT_PENDING_APPROVAL,
        "line_items": line_items,
        "vendor_routing": {
            "primary_vendor_id": DEFAULT_VENDOR_ID,
            "delivery_channel": "email",
            "draft_subject": draft_subject,
            "draft_body_path": relative_export_path(root, draft_abs),
        },
        "summary": {
            "line_item_count": len(line_items),
            "escalated_slot_count": sum(int(item["headcount"]) for item in line_items),
        },
    }
    email_body = _build_email_draft(
        facility_name=facility_name,
        period_start=block_start,
        period_end=block_end,
        line_items=line_items,
        request_id=request_id,
    )

    request_abs.write_text(json.dumps(request_payload, indent=2, sort_keys=True), encoding="utf-8")
    draft_abs.write_text(email_body, encoding="utf-8")

    request_relative = relative_export_path(root, request_abs)
    draft_relative = relative_export_path(root, draft_abs)
    logger.info(
        "Agency worker wrote request=%s draft=%s line_items=%s escalated_slots=%s",
        request_relative,
        draft_relative,
        len(line_items),
        request_payload["summary"]["escalated_slot_count"],
    )

    return AgencyHandoffResult(
        request_path=request_abs,
        email_draft_path=draft_abs,
        request_relative_path=request_relative,
        email_draft_relative_path=draft_relative,
        line_item_count=len(line_items),
        escalated_slot_count=int(request_payload["summary"]["escalated_slot_count"]),
        status=STATUS_DRAFT_PENDING_APPROVAL,
    )


def load_agency_request(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def find_latest_agency_request(
    project_root: Path,
    *,
    schedule_period_id: Optional[str] = None,
) -> Optional[Path]:
    exports_dir = resolve_project_path(project_root, "exports")
    if not exports_dir.is_dir():
        return None
    candidates = sorted(
        exports_dir.glob(f"{AGENCY_REQUEST_PREFIX}_*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        if schedule_period_id is None:
            return candidate
        try:
            payload = load_agency_request(candidate)
        except json.JSONDecodeError:
            continue
        if payload.get("schedule_period_id") == schedule_period_id:
            return candidate
    return None


def mark_agency_request_sent(project_root: Path, request_path: Path | str) -> dict:
    """Persist manager approval by marking the agency request as sent."""

    abs_path, payload = _load_and_prepare_request(project_root, request_path)
    payload["status"] = STATUS_SENT
    payload["sent_at_utc"] = _utc_now_iso()
    _save_agency_request(abs_path, payload)
    logger.info("Agency request marked SENT: %s", relative_export_path(project_root, abs_path))
    return payload


def mark_agency_request_fulfilled(
    project_root: Path,
    request_path: Path | str,
    *,
    vendor_reference: str = "",
    notes: str = "",
    actor: Optional[str] = None,
) -> dict:
    abs_path, payload = _load_and_prepare_request(project_root, request_path)
    payload["status"] = STATUS_FULFILLED
    payload["fulfilled_at_utc"] = _utc_now_iso()
    if vendor_reference.strip():
        payload["vendor_reference"] = vendor_reference.strip()
    if notes.strip():
        payload["fulfillment_notes"] = notes.strip()
    if actor:
        payload["fulfilled_by"] = actor
    for item in payload.get("line_items") or []:
        if item.get("fulfillment_status") not in {LINE_CLOSED_UNFILLED, LINE_FULFILLED}:
            item["fulfillment_status"] = LINE_FULFILLED
    _save_agency_request(abs_path, payload)
    logger.info("Agency request marked FULFILLED: %s", relative_export_path(project_root, abs_path))
    return payload


def mark_agency_request_closed_unfilled(
    project_root: Path,
    request_path: Path | str,
    *,
    reason: str = "",
    actor: Optional[str] = None,
) -> dict:
    abs_path, payload = _load_and_prepare_request(project_root, request_path)
    payload["status"] = STATUS_CLOSED_UNFILLED
    payload["closed_at_utc"] = _utc_now_iso()
    if reason.strip():
        payload["closure_reason"] = reason.strip()
    if actor:
        payload["closed_by"] = actor
    for item in payload.get("line_items") or []:
        if item.get("fulfillment_status") != LINE_FULFILLED:
            item["fulfillment_status"] = LINE_CLOSED_UNFILLED
    _save_agency_request(abs_path, payload)
    logger.info(
        "Agency request marked CLOSED_UNFILLED: %s",
        relative_export_path(project_root, abs_path),
    )
    return payload


def record_agency_placeholder(
    project_root: Path,
    request_path: Path | str,
    *,
    line_item_id_value: str,
    slot_label: str,
    assignee_label: str = DEFAULT_PLACEHOLDER_LABEL,
    mapped_employee_id: Optional[str] = None,
    shift_template_id: Optional[str] = None,
    injected_assignment_id: Optional[str] = None,
    actor: Optional[str] = None,
) -> dict:
    abs_path, payload = _load_and_prepare_request(project_root, request_path)
    target_item: Optional[dict] = None
    for item in payload.get("line_items") or []:
        if line_item_id(item) == line_item_id_value:
            target_item = item
            break
    if target_item is None:
        raise ValueError(f"Unknown agency line item: {line_item_id_value}")

    placements = target_item.setdefault("placements", [])
    assert isinstance(placements, list)
    for existing in placements:
        if str(existing.get("slot_label", "")).strip().casefold() == slot_label.strip().casefold():
            return payload

    placement = {
        "placement_id": f"plc-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
        "agency_request_id": payload.get("request_id"),
        "line_item_id": line_item_id_value,
        "slot_label": slot_label,
        "assignment_date": target_item.get("date"),
        "shift_code": target_item.get("shift_code"),
        "required_qual": target_item.get("required_qual"),
        "fulfillment_type": "EXTERNAL_LOCUM",
        "assignee_label": assignee_label,
        "employee_id": None,
        "mapped_employee_id": mapped_employee_id,
        "shift_template_id": shift_template_id,
        "injected_assignment_id": injected_assignment_id,
        "created_at_utc": _utc_now_iso(),
        "created_by": actor,
        "status": "PLACEHOLDER",
    }
    placements.append(placement)

    slots = target_item.get("slots") or []
    placed_slots = {
        str(entry.get("slot_label", "")).strip().casefold() for entry in placements
    }
    if slots and all(str(slot).strip().casefold() in placed_slots for slot in slots):
        target_item["fulfillment_status"] = LINE_FULFILLED
    else:
        target_item["fulfillment_status"] = LINE_PLACEHOLDER_CREATED

    _sync_request_status(payload)
    _save_agency_request(abs_path, payload)
    logger.info(
        "Agency placeholder recorded for line_item=%s slot=%s",
        line_item_id_value,
        slot_label,
    )
    return payload


def create_line_item_placeholders(
    project_root: Path,
    request_path: Path | str,
    *,
    line_item_id_value: str,
    employees: Sequence[Mapping[str, object]],
    templates: Mapping[str, Mapping[str, object]],
    persist_assignment: Optional[object] = None,
    tenant_id: Optional[str] = None,
    schedule_period_id: Optional[str] = None,
    assignee_label: str = DEFAULT_PLACEHOLDER_LABEL,
    actor: Optional[str] = None,
) -> dict:
    """
    Create placeholder placements for every slot on a grouped agency line item.

    ``persist_assignment`` is an optional callback invoked as
    ``persist_assignment(employee_id, shift_template_id, assignment_date) -> assignment_id``.
    """

    abs_path, payload = _load_and_prepare_request(project_root, request_path)
    target_item: Optional[dict] = None
    for item in payload.get("line_items") or []:
        if line_item_id(item) == line_item_id_value:
            target_item = item
            break
    if target_item is None:
        raise ValueError(f"Unknown agency line item: {line_item_id_value}")

    shift_template = shift_template_id_for_code(templates, str(target_item.get("shift_code", "")))
    if shift_template is None:
        raise ValueError(
            f"No shift template found for shift_code={target_item.get('shift_code')}"
        )
    assignment_day = _parse_iso_date(target_item.get("date"))
    if assignment_day is None:
        raise ValueError(f"Invalid assignment date on line item {line_item_id_value}")

    for slot in target_item.get("slots") or []:
        slot_label = str(slot).strip()
        if not slot_label:
            continue
        mapped_employee_id = resolve_employee_id_for_slot(slot_label, employees)
        if mapped_employee_id is None:
            logger.warning(
                "Agency placeholder could not map slot %s to roster employee; skipping",
                slot_label,
            )
            continue

        injected_assignment_id: Optional[str] = None
        if persist_assignment is not None:
            injected_assignment_id = persist_assignment(
                mapped_employee_id,
                shift_template,
                assignment_day,
            )

        payload = record_agency_placeholder(
            project_root,
            abs_path,
            line_item_id_value=line_item_id_value,
            slot_label=slot_label,
            assignee_label=assignee_label,
            mapped_employee_id=mapped_employee_id,
            shift_template_id=shift_template,
            injected_assignment_id=injected_assignment_id,
            actor=actor,
        )

    return payload
