from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Tuple

CONTRACT_LINE_TYPES: Tuple[str, ...] = ("D/N", "D/E", "M-F")

CRITICAL_CONTRACT_LINE_PREFIX = "CRITICAL: Contract Line Violation"

PORTAGE_ROTATION_LABEL = re.compile(
    r"^\s*(?P<role>MLT|MLA)\s+(?P<seq>\d+)\s*(?:\((?P<fte>[\d.]+)\s+(?P<line>D/N|D/E|M-F)\))?\s*$",
    re.IGNORECASE,
)
PORTAGE_ROTATION_LABEL_COMPACT = re.compile(
    r"^\s*(?P<role>MLT|MLA)\s*\((?P<fte>[\d.]+)\s+(?P<line>D/N|D/E|M-F)\)\s*$",
    re.IGNORECASE,
)


def _looks_like_portage_rotation_label(text: str) -> bool:
    upper = text.upper()
    if "MLT" not in upper and "MLA" not in upper:
        return False
    if re.search(r"\(\s*[\d.]+\s+(D/N|D/E|M-F)\s*\)", text, re.IGNORECASE):
        return True
    return bool(re.search(r"(MLT|MLA)\s+\d+", text, re.IGNORECASE))


@dataclass(frozen=True, slots=True)
class PortageRotationParse:
    role: str
    sequence: str
    fte: Optional[float]
    contract_line_type: Optional[str]
    display_name: str


def normalize_contract_line_type(value: object) -> Optional[str]:
    text = str(value or "").strip().upper().replace(" ", "")
    if not text:
        return None
    mapping = {
        "D/N": "D/N",
        "DN": "D/N",
        "D/E": "D/E",
        "DE": "D/E",
        "M-F": "M-F",
        "MF": "M-F",
        "M/F": "M-F",
    }
    normalized = mapping.get(text.replace("-", "/"))
    if normalized in CONTRACT_LINE_TYPES:
        return normalized
    return None


def normalize_shift_band_code(code: str) -> str:
    token = str(code or "").strip().upper()
    aliases = {
        "M": "MORNING",
        "MORNING": "MORNING",
        "E": "EVENING",
        "EVENING": "EVENING",
        "N": "NIGHT",
        "NIGHT": "NIGHT",
    }
    return aliases.get(token, token)


def allowed_shift_codes_for_contract_line(contract_line_type: str) -> FrozenSet[str]:
    """Generic union bands (legacy). Prefer ``allowed_shift_codes_for_role_contract``."""
    normalized = normalize_contract_line_type(contract_line_type)
    if normalized == "D/E":
        return frozenset({"MORNING", "EVENING"})
    if normalized == "D/N":
        return frozenset({"MORNING", "NIGHT"})
    if normalized == "M-F":
        return frozenset({"MORNING"})
    return frozenset({"MORNING", "EVENING", "NIGHT"})


ROLE_CONTRACT_SHIFT_MATRIX: Dict[Tuple[str, str], FrozenSet[str]] = {
    ("MLT", "D/E"): frozenset({"MORNING", "EVENING"}),
    ("MLT", "D/N"): frozenset({"MORNING", "NIGHT"}),
    ("MLA", "D/E"): frozenset({"MORNING", "EVENING"}),
    ("MLA", "D/N"): frozenset({"MORNING", "NIGHT"}),
    ("MLT", "M-F"): frozenset({"MORNING"}),
    ("MLA", "M-F"): frozenset({"MORNING"}),
}


def allowed_shift_codes_for_role_contract(
    contract_line_type: str,
    *,
    qual_code: str,
) -> FrozenSet[str]:
    """
    Full contractual shift sets per role and line type.

    MLT/MLA D/E → Day (Morning) + Evening · MLT/MLA D/N → Day + Night · M-F → Day only.
    """

    line = normalize_contract_line_type(contract_line_type)
    role = str(qual_code or "").strip().upper()
    if line and role:
        allowed = ROLE_CONTRACT_SHIFT_MATRIX.get((role, line))
        if allowed is not None:
            return allowed
    return allowed_shift_codes_for_contract_line(contract_line_type)


def contract_line_violation_message(
    contract_line_type: str,
    shift_code: str,
    *,
    qual_code: Optional[str] = None,
) -> str:
    line = normalize_contract_line_type(contract_line_type) or contract_line_type
    band = normalize_shift_band_code(shift_code)
    role = str(qual_code or "").strip().upper()

    if line == "D/E" and band == "NIGHT":
        return (
            f"{CRITICAL_CONTRACT_LINE_PREFIX} "
            "(Day/Evening Worker assigned to Night Shift)"
        )
    if line == "D/N" and band == "EVENING":
        return (
            f"{CRITICAL_CONTRACT_LINE_PREFIX} "
            "(Day/Night Worker assigned to Evening Shift)"
        )
    if line == "M-F" and band in {"EVENING", "NIGHT"}:
        label = "Evening" if band == "EVENING" else "Night"
        return (
            f"{CRITICAL_CONTRACT_LINE_PREFIX} "
            f"(Monday–Friday Worker assigned to {label} Shift)"
        )
    return (
        f"{CRITICAL_CONTRACT_LINE_PREFIX} "
        f"({line} contract line ineligible for {band} shift)"
    )


def is_critical_contract_line_violation(message: Optional[str]) -> bool:
    return bool(message and message.startswith(CRITICAL_CONTRACT_LINE_PREFIX))


def parse_portage_rotation_label(text: str) -> Optional[PortageRotationParse]:
    cleaned = " ".join(str(text or "").strip().split())
    if not cleaned:
        return None
    if not _looks_like_portage_rotation_label(cleaned):
        return None

    match = PORTAGE_ROTATION_LABEL.match(cleaned) or PORTAGE_ROTATION_LABEL_COMPACT.match(
        cleaned
    )
    if not match:
        return None

    role = match.group("role").upper()
    seq = match.groupdict().get("seq") or ""
    fte_raw = match.groupdict().get("fte")
    line_raw = match.groupdict().get("line")
    fte = float(fte_raw) if fte_raw else None
    contract_line = normalize_contract_line_type(line_raw) if line_raw else None
    display = f"{role} {seq}".strip() if seq else role
    if fte is not None and contract_line:
        display = f"{display} ({fte:g} {contract_line})"

    return PortageRotationParse(
        role=role,
        sequence=seq or "0",
        fte=fte,
        contract_line_type=contract_line,
        display_name=display,
    )


def ensure_contract_line_schema(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(employees)")}
    if "contract_line_type" not in cols:
        conn.execute(
            """
            ALTER TABLE employees
            ADD COLUMN contract_line_type TEXT
            CHECK (
              contract_line_type IS NULL
              OR contract_line_type IN ('D/N', 'D/E', 'M-F')
            )
            """
        )
