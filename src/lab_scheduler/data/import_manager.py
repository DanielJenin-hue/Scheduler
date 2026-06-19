from __future__ import annotations

import io
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from lab_scheduler.models.employee import (
    CONTRACT_LINE_TYPES,
    parse_portage_rotation_label,
)

STANDARD_WEEKLY_HOURS_AT_1_0_FTE = 40.0
# Portage-valid FTE tiers only (0.8 / 32h removed).
FTE_CONTRACT_TIERS: Tuple[float, ...] = (1.0, 0.7, 0.6, 0.5, 0.4, 0.2)
FUZZY_MATCH_THRESHOLD = 0.72

CANONICAL_COLUMNS = {
    "full_name": ("full name", "name", "employee name", "employee"),
    "role": ("role", "role (mlt/mla)", "qualification", "credential", "tier"),
    "seniority_hours": ("seniority hours", "seniority", "seniority hrs", "seniority_hrs"),
    "target_weekly_hours": (
        "target weekly hours",
        "weekly hours",
        "target hours",
        "hours per week",
        "weekly target",
    ),
}


class RosterImportError(Exception):
    """Raised when a roster file cannot be parsed or validated."""


@dataclass(frozen=True, slots=True)
class ExistingEmployeeRecord:
    id: str
    full_name: str
    first_name: str
    last_name: str


@dataclass
class ImportRowPreview:
    row_number: int
    full_name: str
    first_name: str
    last_name: str
    role_code: str
    seniority_hours: Optional[float]
    target_weekly_hours: float
    fte: float
    needs_seniority_manual: bool
    contract_line_type: Optional[str] = None
    validation_errors: List[str] = field(default_factory=list)
    matched_existing_id: Optional[str] = None
    matched_existing_name: Optional[str] = None
    match_confidence: float = 0.0
    action: str = "insert"


@dataclass
class ImportPreview:
    rows: List[ImportRowPreview]
    source_filename: str

    @property
    def valid_rows(self) -> List[ImportRowPreview]:
        return [row for row in self.rows if not row.validation_errors]

    @property
    def error_count(self) -> int:
        return sum(1 for row in self.rows if row.validation_errors)

    @property
    def needs_seniority_count(self) -> int:
        return sum(1 for row in self.valid_rows if row.needs_seniority_manual)

    @property
    def insert_count(self) -> int:
        return sum(1 for row in self.valid_rows if row.action == "insert")

    @property
    def update_count(self) -> int:
        return sum(1 for row in self.valid_rows if row.action == "update")

    @property
    def can_commit(self) -> bool:
        if self.error_count:
            return False
        return all(
            not row.needs_seniority_manual or row.seniority_hours is not None
            for row in self.valid_rows
        )


@dataclass(frozen=True, slots=True)
class ImportCommitResult:
    inserted: int
    updated: int
    skipped: int


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_header(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _normalize_person_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.strip().lower())


def _parse_full_name(full_name: str) -> Optional[Tuple[str, str]]:
    cleaned = " ".join(str(full_name or "").strip().split())
    if len(cleaned) < 2:
        return None
    parts = cleaned.split(" ")
    if len(parts) < 2:
        return None
    return parts[0], " ".join(parts[1:])


def _normalize_role(value: object) -> Optional[str]:
    text = str(value or "").strip().upper()
    if not text:
        return None
    if "MLT" in text:
        return "MLT"
    if "MLA" in text:
        return "MLA"
    token = re.sub(r"[^A-Z]", "", text)
    if token in {"MLT", "MLA"}:
        return token
    return None


def _snap_fte(raw_fte: float) -> float:
    bounded = max(min(raw_fte, 1.0), 0.05)
    return min(FTE_CONTRACT_TIERS, key=lambda tier: abs(tier - bounded))


def fte_from_target_weekly_hours(
    target_weekly_hours: float,
    *,
    standard_weekly_hours: float = STANDARD_WEEKLY_HOURS_AT_1_0_FTE,
) -> float:
    if standard_weekly_hours <= 0:
        raise ValueError("standard_weekly_hours must be positive")
    return _snap_fte(target_weekly_hours / standard_weekly_hours)


def _name_similarity(left: str, right: str) -> float:
    left_norm = _normalize_person_token(left)
    right_norm = _normalize_person_token(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    if len(left_norm) >= 3 and (left_norm in right_norm or right_norm in left_norm):
        return 0.92
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def fuzzy_match_existing_employee(
    full_name: str,
    existing_employees: Sequence[ExistingEmployeeRecord],
) -> Optional[Tuple[ExistingEmployeeRecord, float]]:
    parsed = _parse_full_name(full_name)
    if parsed is None:
        return None
    import_first, import_last = parsed
    best: Optional[Tuple[ExistingEmployeeRecord, float]] = None

    for employee in existing_employees:
        last_score = _name_similarity(import_last, employee.last_name)
        first_score = _name_similarity(import_first, employee.first_name)
        full_score = _name_similarity(full_name, employee.full_name)

        if last_score >= 0.88:
            score = 0.55 * last_score + 0.45 * first_score
        else:
            score = full_score

        if best is None or score > best[1]:
            best = (employee, score)

    if best is None or best[1] < FUZZY_MATCH_THRESHOLD:
        return None
    return best


def _map_columns(columns: Iterable[object]) -> Dict[str, str]:
    normalized = {_normalize_header(column): str(column) for column in columns}
    mapped: Dict[str, str] = {}
    for canonical, aliases in CANONICAL_COLUMNS.items():
        for alias in aliases:
            if alias in normalized:
                mapped[canonical] = normalized[alias]
                break
    return mapped


def _coerce_float(value: object) -> Optional[float]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "—", "-"}:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def parse_roster_file(*, content: bytes, filename: str) -> pd.DataFrame:
    lowered = filename.lower()
    buffer = io.BytesIO(content)
    try:
        if lowered.endswith(".csv"):
            frame = pd.read_csv(buffer)
        elif lowered.endswith((".xlsx", ".xls")):
            frame = pd.read_excel(buffer)
        else:
            raise RosterImportError("Unsupported file type. Upload a .csv or .xlsx roster file.")
    except ImportError as exc:
        raise RosterImportError(
            "Excel import requires the optional `openpyxl` package. "
            "Install app dependencies or upload CSV instead."
        ) from exc
    except Exception as exc:
        raise RosterImportError(f"Could not read roster file: {exc}") from exc

    if frame.empty:
        raise RosterImportError("The uploaded roster file is empty.")
    return frame


def build_import_preview(
    frame: pd.DataFrame,
    *,
    source_filename: str,
    existing_employees: Sequence[ExistingEmployeeRecord],
    standard_weekly_hours: float = STANDARD_WEEKLY_HOURS_AT_1_0_FTE,
) -> ImportPreview:
    column_map = _map_columns(frame.columns)
    if "full_name" not in column_map:
        raise RosterImportError("Missing required column(s): 'full name'")

    previews: List[ImportRowPreview] = []
    for index, row in frame.iterrows():
        row_number = int(index) + 2
        full_name = str(row.get(column_map["full_name"], "")).strip()
        if full_name.lower() == "nan":
            full_name = ""
        role_raw = (
            str(row.get(column_map["role"], "")).strip()
            if "role" in column_map
            else ""
        )
        if role_raw.lower() == "nan":
            role_raw = ""

        portage = parse_portage_rotation_label(full_name)
        if portage is None and role_raw:
            portage = parse_portage_rotation_label(role_raw)

        role_code = _normalize_role(role_raw)
        seniority_raw = (
            _coerce_float(row.get(column_map["seniority_hours"]))
            if "seniority_hours" in column_map
            else None
        )
        target_weekly = (
            _coerce_float(row.get(column_map["target_weekly_hours"]))
            if "target_weekly_hours" in column_map
            else None
        )

        preview = ImportRowPreview(
            row_number=row_number,
            full_name=full_name or (portage.display_name if portage else ""),
            first_name="",
            last_name="",
            role_code=role_code or (portage.role if portage else ""),
            seniority_hours=seniority_raw,
            target_weekly_hours=target_weekly or 0.0,
            fte=0.0,
            contract_line_type=portage.contract_line_type if portage else None,
            needs_seniority_manual=seniority_raw is None,
        )

        if portage is not None:
            preview.full_name = portage.display_name
            preview.first_name = portage.role
            preview.last_name = portage.sequence
            preview.role_code = portage.role
            if portage.contract_line_type:
                preview.contract_line_type = portage.contract_line_type
            if portage.fte is not None:
                preview.fte = _snap_fte(portage.fte)
                preview.target_weekly_hours = round(
                    preview.fte * standard_weekly_hours,
                    2,
                )
                preview.needs_seniority_manual = seniority_raw is None

        if not preview.full_name:
            preview.validation_errors.append("Full Name is required.")
        elif portage is None:
            parsed = _parse_full_name(full_name)
            if parsed is None:
                preview.validation_errors.append("Full Name must include first and last name.")
            else:
                preview.first_name, preview.last_name = parsed

        if preview.role_code not in {"MLT", "MLA"}:
            preview.validation_errors.append("Role must be MLT or MLA.")

        if preview.full_name and preview.fte <= 0:
            if target_weekly is None or target_weekly <= 0:
                if portage is None or portage.fte is None:
                    preview.validation_errors.append(
                        "Target Weekly Hours must be a positive number."
                    )
            else:
                preview.target_weekly_hours = target_weekly
                preview.fte = fte_from_target_weekly_hours(
                    target_weekly,
                    standard_weekly_hours=standard_weekly_hours,
                )

        if preview.contract_line_type and preview.contract_line_type not in CONTRACT_LINE_TYPES:
            preview.validation_errors.append(
                f"Contract line must be one of {', '.join(CONTRACT_LINE_TYPES)}."
            )

        if seniority_raw is not None and seniority_raw < 0:
            preview.validation_errors.append("Seniority Hours cannot be negative.")

        if not preview.validation_errors:
            match = fuzzy_match_existing_employee(preview.full_name, existing_employees)
            if match is not None:
                employee, confidence = match
                preview.matched_existing_id = employee.id
                preview.matched_existing_name = employee.full_name
                preview.match_confidence = confidence
                preview.action = "update"

        previews.append(preview)

    return ImportPreview(rows=previews, source_filename=source_filename)


def preview_to_dict(preview: ImportPreview) -> Dict[str, object]:
    return {
        "source_filename": preview.source_filename,
        "rows": [
            {
                "row_number": row.row_number,
                "full_name": row.full_name,
                "first_name": row.first_name,
                "last_name": row.last_name,
                "role_code": row.role_code,
                "seniority_hours": row.seniority_hours,
                "target_weekly_hours": row.target_weekly_hours,
                "fte": row.fte,
                "contract_line_type": row.contract_line_type,
                "needs_seniority_manual": row.needs_seniority_manual,
                "validation_errors": list(row.validation_errors),
                "matched_existing_id": row.matched_existing_id,
                "matched_existing_name": row.matched_existing_name,
                "match_confidence": row.match_confidence,
                "action": row.action,
            }
            for row in preview.rows
        ],
    }


def preview_from_dict(payload: Mapping[str, object]) -> ImportPreview:
    rows = []
    for item in payload.get("rows", []):
        if not isinstance(item, Mapping):
            continue
        rows.append(
            ImportRowPreview(
                row_number=int(item["row_number"]),
                full_name=str(item["full_name"]),
                first_name=str(item.get("first_name", "")),
                last_name=str(item.get("last_name", "")),
                role_code=str(item.get("role_code", "")),
                seniority_hours=(
                    float(item["seniority_hours"])
                    if item.get("seniority_hours") is not None
                    else None
                ),
                target_weekly_hours=float(item.get("target_weekly_hours", 0.0)),
                fte=float(item.get("fte", 0.0)),
                contract_line_type=(
                    str(item["contract_line_type"])
                    if item.get("contract_line_type")
                    else None
                ),
                needs_seniority_manual=bool(item.get("needs_seniority_manual", False)),
                validation_errors=list(item.get("validation_errors", [])),
                matched_existing_id=(
                    str(item["matched_existing_id"])
                    if item.get("matched_existing_id")
                    else None
                ),
                matched_existing_name=(
                    str(item["matched_existing_name"])
                    if item.get("matched_existing_name")
                    else None
                ),
                match_confidence=float(item.get("match_confidence", 0.0)),
                action=str(item.get("action", "insert")),
            )
        )
    return ImportPreview(rows=rows, source_filename=str(payload.get("source_filename", "")))


def _default_hourly_rate(role_code: str) -> float:
    if role_code == "MLA":
        return 26.0
    return 40.0


def next_employee_code(conn: sqlite3.Connection, tenant_id: str) -> str:
    """Return the next unused tenant-scoped employee code (E1001, E1002, …)."""

    row = conn.execute(
        """
        SELECT employee_code
        FROM employees
        WHERE tenant_id = ? AND employee_code GLOB 'E[0-9]*'
        ORDER BY CAST(substr(employee_code, 2) AS INTEGER) DESC
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    if row and row[0]:
        try:
            candidate = int(str(row[0])[1:]) + 1
        except ValueError:
            candidate = None
    else:
        candidate = None

    if candidate is None:
        count = conn.execute(
            "SELECT COUNT(*) FROM employees WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()[0]
        candidate = 1001 + int(count)

    while True:
        code = f"E{candidate}"
        exists = conn.execute(
            """
            SELECT 1 FROM employees
            WHERE tenant_id = ? AND employee_code = ?
            LIMIT 1
            """,
            (tenant_id, code),
        ).fetchone()
        if not exists:
            return code
        candidate += 1


def commit_import_preview(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    preview: ImportPreview,
    qualification_ids: Mapping[str, str],
    hire_date: Optional[date] = None,
) -> ImportCommitResult:
    if not preview.can_commit:
        raise RosterImportError(
            "Import preview has validation errors or missing seniority values."
        )

    hire = hire_date or date.today()
    now = _utc_now_iso()
    inserted = 0
    updated = 0
    skipped = 0

    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in preview.rows:
            if row.validation_errors:
                skipped += 1
                continue

            qual_id = qualification_ids.get(row.role_code)
            if not qual_id:
                raise RosterImportError(f"No qualification configured for role {row.role_code}.")

            seniority = float(row.seniority_hours or 0.0)
            hourly_rate = _default_hourly_rate(row.role_code)

            if row.action == "update" and row.matched_existing_id:
                employee_id = row.matched_existing_id
                conn.execute(
                    """
                    UPDATE employees
                    SET first_name = ?, last_name = ?, fte = ?, base_hourly_rate = ?,
                        seniority_hours = ?, contract_line_type = ?, is_active = 1, updated_at = ?
                    WHERE tenant_id = ? AND id = ?
                    """,
                    (
                        row.first_name,
                        row.last_name,
                        row.fte,
                        hourly_rate,
                        seniority,
                        row.contract_line_type,
                        now,
                        tenant_id,
                        employee_id,
                    ),
                )
                conn.execute(
                    """
                    DELETE FROM employee_qualifications
                    WHERE tenant_id = ? AND employee_id = ?
                    """,
                    (tenant_id, employee_id),
                )
                conn.execute(
                    """
                    INSERT INTO employee_qualifications (
                      tenant_id, employee_id, qualification_id, awarded_on, expires_on, created_at
                    ) VALUES (?, ?, ?, ?, NULL, ?)
                    """,
                    (tenant_id, employee_id, qual_id, hire.isoformat(), now),
                )
                updated += 1
                continue

            employee_id = f"emp-{uuid.uuid4().hex[:10]}"
            employee_code = next_employee_code(conn, tenant_id)
            conn.execute(
                """
                INSERT INTO employees (
                  id, tenant_id, employee_code, first_name, last_name,
                  hire_date, fte, base_hourly_rate, seniority_hours, contract_line_type,
                  is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    employee_id,
                    tenant_id,
                    employee_code,
                    row.first_name,
                    row.last_name,
                    hire.isoformat(),
                    row.fte,
                    hourly_rate,
                    seniority,
                    row.contract_line_type,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO employee_qualifications (
                  tenant_id, employee_id, qualification_id, awarded_on, expires_on, created_at
                ) VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (tenant_id, employee_id, qual_id, hire.isoformat(), now),
            )
            inserted += 1

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return ImportCommitResult(inserted=inserted, updated=updated, skipped=skipped)
