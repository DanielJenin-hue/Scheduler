"""CRUD and workflow operations for business prospects."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional, Sequence

from lab_scheduler.auth.onboarding import DEFAULT_JURISDICTION, seed_lab_infrastructure
from lab_scheduler.auth.signup import slugify_facility_name
from lab_scheduler.business.discovery import discover_manitoba_prospects, purge_excluded_prospects
from lab_scheduler.business.email_templates import (
    EmailDraft,
    default_outreach_sender_name,
    generate_outreach_email,
)
from lab_scheduler.business.models import (
    Prospect,
    ProspectStatus,
    ensure_business_prospects_schema,
    serialize_pain_signals,
    utc_now_iso,
)
from lab_scheduler.tenant.configuration import (
    ensure_tenant_configuration_schema,
    set_tenant_config_value,
)

__all__ = [
    "ProspectServiceError",
    "ProceedClientResult",
    "create_prospect",
    "discover_and_persist_manitoba",
    "generate_email_preview",
    "get_prospect",
    "list_prospects",
    "proceed_with_client",
    "update_prospect",
    "update_prospect_status",
]

ONBOARDING_COMPLETE_KEY = "onboarding_complete"


class ProspectServiceError(ValueError):
    """Raised when a prospect workflow operation is invalid."""


@dataclass(frozen=True, slots=True)
class ProceedClientResult:
    prospect: Prospect
    tenant_id: str
    tenant_created: bool


def _clamp_icp(score: int) -> int:
    return int(max(0, min(100, score)))


def _validate_status_transition(current: ProspectStatus, new: ProspectStatus) -> None:
    if current == new:
        return
    if current == ProspectStatus.DECLINED and new != ProspectStatus.DECLINED:
        raise ProspectServiceError("Declined prospects cannot change status without manual reset.")
    if current == ProspectStatus.ACTIVE_CLIENT and new not in {
        ProspectStatus.ACTIVE_CLIENT,
        ProspectStatus.DECLINED,
    }:
        raise ProspectServiceError("Active clients can only remain active or be marked declined.")


def create_prospect(
    conn: sqlite3.Connection,
    *,
    facility: str,
    province: str = "MB",
    facility_id: str | None = None,
    contact_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    icp_score: int = 0,
    pain_signals: Sequence[str] | None = None,
    status: ProspectStatus | str = ProspectStatus.DISCOVERED,
    notes: str | None = None,
) -> Prospect:
    ensure_business_prospects_schema(conn)
    if not facility.strip():
        raise ProspectServiceError("Facility name is required.")

    if facility_id:
        existing = conn.execute(
            "SELECT id FROM business_prospects WHERE facility_id = ?",
            (facility_id,),
        ).fetchone()
        if existing is not None:
            raise ProspectServiceError(f"Prospect already exists for facility_id {facility_id!r}.")

    now = utc_now_iso()
    prospect_id = f"prospect-{uuid.uuid4().hex[:12]}"
    normalized_status = ProspectStatus.normalize(status)

    conn.execute(
        """
        INSERT INTO business_prospects (
          id, facility_id, facility, contact_name, email, phone, province,
          icp_score, pain_signals_json, status, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            prospect_id,
            facility_id,
            facility.strip(),
            contact_name,
            email,
            phone,
            province.strip().upper()[:8] or "MB",
            _clamp_icp(icp_score),
            serialize_pain_signals(pain_signals or []),
            normalized_status.value,
            notes,
            now,
            now,
        ),
    )
    conn.commit()
    return get_prospect(conn, prospect_id)


def get_prospect(conn: sqlite3.Connection, prospect_id: str) -> Prospect:
    ensure_business_prospects_schema(conn)
    row = conn.execute(
        "SELECT * FROM business_prospects WHERE id = ?",
        (prospect_id,),
    ).fetchone()
    if row is None:
        raise ProspectServiceError(f"Prospect {prospect_id!r} not found.")
    return Prospect.from_row(row)


def list_prospects(
    conn: sqlite3.Connection,
    *,
    status: ProspectStatus | str | None = None,
    province: str | None = None,
    min_icp_score: int | None = None,
    limit: int | None = None,
) -> List[Prospect]:
    ensure_business_prospects_schema(conn)
    clauses = ["1=1"]
    params: list[object] = []

    if status is not None:
        clauses.append("status = ?")
        params.append(ProspectStatus.normalize(status).value)
    if province is not None:
        clauses.append("province = ?")
        params.append(province.strip().upper())
    if min_icp_score is not None:
        clauses.append("icp_score >= ?")
        params.append(int(min_icp_score))

    sql = (
        "SELECT * FROM business_prospects WHERE "
        + " AND ".join(clauses)
        + " ORDER BY icp_score DESC, facility ASC"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))

    rows = conn.execute(sql, params).fetchall()
    return [Prospect.from_row(row) for row in rows]


def update_prospect(
    conn: sqlite3.Connection,
    prospect_id: str,
    **fields: object,
) -> Prospect:
    ensure_business_prospects_schema(conn)
    current = get_prospect(conn, prospect_id)

    allowed = {
        "facility",
        "facility_id",
        "contact_name",
        "email",
        "phone",
        "province",
        "icp_score",
        "pain_signals",
        "status",
        "notes",
        "email_draft_subject",
        "email_draft_body",
        "tenant_id",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ProspectServiceError(f"Unknown prospect fields: {', '.join(sorted(unknown))}")
    if not fields:
        return current

    updates: dict[str, object] = {}
    if "facility" in fields and fields["facility"] is not None:
        updates["facility"] = str(fields["facility"]).strip()
    if "facility_id" in fields:
        updates["facility_id"] = fields["facility_id"]
    if "contact_name" in fields:
        updates["contact_name"] = fields["contact_name"]
    if "email" in fields:
        updates["email"] = fields["email"]
    if "phone" in fields:
        updates["phone"] = fields["phone"]
    if "province" in fields and fields["province"] is not None:
        updates["province"] = str(fields["province"]).strip().upper()
    if "icp_score" in fields and fields["icp_score"] is not None:
        updates["icp_score"] = _clamp_icp(int(fields["icp_score"]))
    if "pain_signals" in fields and fields["pain_signals"] is not None:
        updates["pain_signals_json"] = serialize_pain_signals(fields["pain_signals"])  # type: ignore[arg-type]
    if "status" in fields and fields["status"] is not None:
        new_status = ProspectStatus.normalize(fields["status"])  # type: ignore[arg-type]
        _validate_status_transition(current.status, new_status)
        updates["status"] = new_status.value
    if "notes" in fields:
        updates["notes"] = fields["notes"]
    if "email_draft_subject" in fields:
        updates["email_draft_subject"] = fields["email_draft_subject"]
    if "email_draft_body" in fields:
        updates["email_draft_body"] = fields["email_draft_body"]
    if "tenant_id" in fields:
        updates["tenant_id"] = fields["tenant_id"]

    if not updates:
        return current

    updates["updated_at"] = utc_now_iso()
    set_clause = ", ".join(f"{column} = ?" for column in updates)
    conn.execute(
        f"UPDATE business_prospects SET {set_clause} WHERE id = ?",
        (*updates.values(), prospect_id),
    )
    conn.commit()
    return get_prospect(conn, prospect_id)


def update_prospect_status(
    conn: sqlite3.Connection,
    prospect_id: str,
    status: ProspectStatus | str,
) -> Prospect:
    return update_prospect(conn, prospect_id, status=status)


def generate_email_preview(
    conn: sqlite3.Connection,
    prospect_id: str,
    *,
    sender_name: str | None = None,
    mark_previewed: bool = True,
    include_pricing: bool = False,
) -> EmailDraft:
    """Generate outreach email and persist draft fields on the prospect."""

    prospect = get_prospect(conn, prospect_id)
    resolved_sender = (sender_name or default_outreach_sender_name()).strip()
    draft = generate_outreach_email(
        prospect,
        sender_name=resolved_sender,
        include_pricing=include_pricing,
    )
    status = prospect.status
    if mark_previewed and status == ProspectStatus.DISCOVERED:
        status = ProspectStatus.PREVIEWED

    update_prospect(
        conn,
        prospect_id,
        email_draft_subject=draft.subject,
        email_draft_body=draft.body,
        status=status,
    )
    return draft


def discover_and_persist_manitoba(
    conn: sqlite3.Connection,
    *,
    skip_existing: bool = False,
    min_icp_score: int = 0,
):
    purge_excluded_prospects(conn)
    return discover_manitoba_prospects(
        conn,
        skip_existing=skip_existing,
        min_icp_score=min_icp_score,
    )


def _unique_slug(conn: sqlite3.Connection, base_slug: str) -> str:
    candidate = base_slug
    suffix = 2
    while conn.execute("SELECT 1 FROM tenants WHERE slug = ?", (candidate,)).fetchone():
        candidate = f"{base_slug}-{suffix}"
        suffix += 1
        if suffix > 999:
            candidate = f"{base_slug}-{uuid.uuid4().hex[:6]}"
            break
    return candidate


def _create_tenant_for_prospect(conn: sqlite3.Connection, prospect: Prospect) -> str:
    ensure_tenant_configuration_schema(conn)
    tenant_id = f"tenant-{uuid.uuid4().hex[:12]}"
    slug = _unique_slug(conn, slugify_facility_name(prospect.facility))
    now = utc_now_iso()
    trial_ends = (date.today() + timedelta(days=14)).isoformat()

    conn.execute(
        """
        INSERT INTO tenants (
          id, name, slug, status, subscription_status,
          stripe_customer_id, trial_ends_at, created_at, updated_at
        ) VALUES (?, ?, ?, 'active', 'trial', NULL, ?, ?, ?)
        """,
        (tenant_id, prospect.facility, slug, trial_ends, now, now),
    )
    seed_lab_infrastructure(conn, tenant_id=tenant_id)
    set_tenant_config_value(
        conn,
        tenant_id=tenant_id,
        config_key=ONBOARDING_COMPLETE_KEY,
        config_value="0",
    )
    set_tenant_config_value(
        conn,
        tenant_id=tenant_id,
        config_key="jurisdiction",
        config_value=DEFAULT_JURISDICTION,
    )
    if prospect.facility_id:
        set_tenant_config_value(
            conn,
            tenant_id=tenant_id,
            config_key="source_facility_id",
            config_value=prospect.facility_id,
        )
    return tenant_id


def proceed_with_client(
    conn: sqlite3.Connection,
    prospect_id: str,
    *,
    tenant_id: str | None = None,
    create_tenant: bool = False,
) -> ProceedClientResult:
    """Advance a prospect to ``active_client`` and optionally link or create a tenant."""

    if tenant_id and create_tenant:
        raise ProspectServiceError("Provide either tenant_id or create_tenant, not both.")

    prospect = get_prospect(conn, prospect_id)
    if prospect.status == ProspectStatus.DECLINED:
        raise ProspectServiceError("Cannot proceed with a declined prospect.")

    tenant_created = False
    resolved_tenant_id = tenant_id or prospect.tenant_id

    if create_tenant:
        resolved_tenant_id = _create_tenant_for_prospect(conn, prospect)
        tenant_created = True
    elif resolved_tenant_id:
        row = conn.execute(
            "SELECT id FROM tenants WHERE id = ?",
            (resolved_tenant_id,),
        ).fetchone()
        if row is None:
            raise ProspectServiceError(f"Tenant {resolved_tenant_id!r} not found.")
    else:
        raise ProspectServiceError(
            "Provide tenant_id to link an existing workspace, or set create_tenant=True."
        )

    updated = update_prospect(
        conn,
        prospect_id,
        status=ProspectStatus.ACTIVE_CLIENT,
        tenant_id=resolved_tenant_id,
    )
    conn.commit()
    return ProceedClientResult(
        prospect=updated,
        tenant_id=resolved_tenant_id,
        tenant_created=tenant_created,
    )
