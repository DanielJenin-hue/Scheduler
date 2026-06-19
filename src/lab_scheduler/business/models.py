"""Business prospect / lead models and schema helpers."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, List, Optional, Sequence

__all__ = [
    "ProspectStatus",
    "PROSPECT_STATUSES",
    "Prospect",
    "ensure_business_prospects_schema",
    "parse_pain_signals",
    "serialize_pain_signals",
    "utc_now_iso",
]

PROSPECT_STATUSES: tuple[str, ...] = (
    "discovered",
    "previewed",
    "contacted",
    "active_client",
    "declined",
)


class ProspectStatus(str, Enum):
    DISCOVERED = "discovered"
    PREVIEWED = "previewed"
    CONTACTED = "contacted"
    ACTIVE_CLIENT = "active_client"
    DECLINED = "declined"

    @classmethod
    def normalize(cls, value: str | ProspectStatus) -> ProspectStatus:
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        try:
            return cls(text)
        except ValueError as exc:
            allowed = ", ".join(PROSPECT_STATUSES)
            raise ValueError(f"Invalid prospect status {value!r}; expected one of: {allowed}") from exc


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_pain_signals(raw: object) -> List[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return [text]
    if not isinstance(parsed, list):
        return [str(parsed)]
    return [str(item).strip() for item in parsed if str(item).strip()]


def serialize_pain_signals(signals: Sequence[str]) -> str:
    cleaned = [str(item).strip() for item in signals if str(item).strip()]
    return json.dumps(cleaned)


@dataclass(slots=True)
class Prospect:
    id: str
    facility: str
    province: str = "MB"
    facility_id: Optional[str] = None
    contact_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    icp_score: int = 0
    pain_signals: List[str] = field(default_factory=list)
    status: ProspectStatus = ProspectStatus.DISCOVERED
    notes: Optional[str] = None
    email_draft_subject: Optional[str] = None
    email_draft_body: Optional[str] = None
    tenant_id: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "facility_id": self.facility_id,
            "facility": self.facility,
            "contact_name": self.contact_name,
            "email": self.email,
            "phone": self.phone,
            "province": self.province,
            "icp_score": self.icp_score,
            "pain_signals": list(self.pain_signals),
            "status": self.status.value,
            "notes": self.notes,
            "email_draft_subject": self.email_draft_subject,
            "email_draft_body": self.email_draft_body,
            "tenant_id": self.tenant_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row | tuple | dict) -> Prospect:
        if isinstance(row, sqlite3.Row):
            data = dict(row)
        elif isinstance(row, dict):
            data = row
        else:
            (
                pid,
                facility_id,
                facility,
                contact_name,
                email,
                phone,
                province,
                icp_score,
                pain_signals_json,
                status,
                notes,
                email_draft_subject,
                email_draft_body,
                tenant_id,
                created_at,
                updated_at,
            ) = row
            data = {
                "id": pid,
                "facility_id": facility_id,
                "facility": facility,
                "contact_name": contact_name,
                "email": email,
                "phone": phone,
                "province": province,
                "icp_score": icp_score,
                "pain_signals_json": pain_signals_json,
                "status": status,
                "notes": notes,
                "email_draft_subject": email_draft_subject,
                "email_draft_body": email_draft_body,
                "tenant_id": tenant_id,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        return cls(
            id=str(data["id"]),
            facility_id=data.get("facility_id"),
            facility=str(data["facility"]),
            contact_name=data.get("contact_name"),
            email=data.get("email"),
            phone=data.get("phone"),
            province=str(data.get("province") or "MB"),
            icp_score=int(data.get("icp_score") or 0),
            pain_signals=parse_pain_signals(data.get("pain_signals_json")),
            status=ProspectStatus.normalize(str(data.get("status") or ProspectStatus.DISCOVERED.value)),
            notes=data.get("notes"),
            email_draft_subject=data.get("email_draft_subject"),
            email_draft_body=data.get("email_draft_body"),
            tenant_id=data.get("tenant_id"),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )


def ensure_business_prospects_schema(conn: sqlite3.Connection) -> None:
    """Create ``business_prospects`` if missing (idempotent)."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS business_prospects (
          id                   TEXT PRIMARY KEY,
          facility_id          TEXT,
          facility             TEXT NOT NULL,
          contact_name         TEXT,
          email                TEXT,
          phone                TEXT,
          province             TEXT NOT NULL DEFAULT 'MB',
          icp_score            INTEGER NOT NULL DEFAULT 0
                                 CHECK (icp_score >= 0 AND icp_score <= 100),
          pain_signals_json    TEXT NOT NULL DEFAULT '[]',
          status               TEXT NOT NULL DEFAULT 'discovered'
                                 CHECK (status IN (
                                   'discovered',
                                   'previewed',
                                   'contacted',
                                   'active_client',
                                   'declined'
                                 )),
          notes                TEXT,
          email_draft_subject  TEXT,
          email_draft_body     TEXT,
          tenant_id            TEXT REFERENCES tenants(id) ON DELETE SET NULL,
          created_at           TEXT NOT NULL,
          updated_at           TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_business_prospects_facility_id
          ON business_prospects (facility_id)
          WHERE facility_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_prospects_status
          ON business_prospects (status, icp_score DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_prospects_province
          ON business_prospects (province)
        """
    )
