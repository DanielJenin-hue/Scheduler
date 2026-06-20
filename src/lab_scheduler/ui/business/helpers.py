"""Shared helpers for the Business operator UI."""

from __future__ import annotations

import html
import json
import sqlite3
import textwrap
import urllib.parse
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Optional

from lab_scheduler.business.discovery import DEFAULT_FACILITY_DATASET, score_facility_record
from lab_scheduler.business.email_templates import (
    MANAGED_BLOCK_PRICE_LABEL,
    PRO_MONTHLY_PRICE_LABEL,
)
from lab_scheduler.business.models import Prospect, ProspectStatus
from lab_scheduler.rsi.prospector import RegionalFacilityRecord, load_regional_facility_dataset

__all__ = [
    "DEFAULT_EMAIL_BODY_TEMPLATE",
    "DEFAULT_EMAIL_SUBJECT_TEMPLATE",
    "FacilityEnrichment",
    "ONBOARDING_CONFIG_KEY",
    "STATUS_LABELS",
    "FIRST_TOUCH_SUBJECT_VARIANT_LABELS",
    "blocked_honesty_phrases",
    "build_template_context",
    "first_touch_subject",
    "derive_pitch_angle",
    "email_preview_envelope_html",
    "format_test_volume",
    "format_volume_short",
    "icp_band",
    "icp_display_score",
    "load_facility_enrichment",
    "load_onboarding_tasks",
    "mailto_link",
    "inbound_reply_to_address",
    "merge_template_variables",
    "save_onboarding_tasks",
    "status_badge_class",
    "status_label",
]

STATUS_LABELS: dict[ProspectStatus, str] = {
    ProspectStatus.DISCOVERED: "New",
    ProspectStatus.PREVIEWED: "Previewed",
    ProspectStatus.CONTACTED: "Previewed",
    ProspectStatus.ACTIVE_CLIENT: "Active Client",
    ProspectStatus.DECLINED: "Passed",
}

ONBOARDING_CONFIG_KEY = "business_onboarding_tasks"

DEFAULT_ONBOARDING_TASKS: tuple[tuple[str, str], ...] = (
    ("send_outreach", "Send outreach email"),
    ("create_tenant", "Create tenant"),
    ("collect_roster", "Collect roster CSV"),
    ("create_period", "Create schedule period"),
    ("distribute_fill_save", "Run Distribute / Fill / Save"),
    ("rsi_gate", "RSI gate pass"),
    ("deliver_breakroom", "Deliver breakroom HTML"),
    ("invoice", "Invoice first block"),
)

HONESTY_BLOCKLIST = (
    "hipaa certified",
    "used by",
    "trusted by",
    "partner hospital",
    "certified compliant",
)


@dataclass(frozen=True, slots=True)
class FacilityEnrichment:
    facility_id: Optional[str]
    region: str
    province: str
    annual_test_volume: int
    mlt_fte: float
    mla_fte: float
    deployment_score: float
    estimated_savings_usd: float
    rationale: str

    @property
    def roster_summary(self) -> str:
        total = self.mlt_fte + self.mla_fte
        return f"{total:.0f} FTE ({self.mlt_fte:.0f} MLT · {self.mla_fte:.0f} MLA)"

    @property
    def mlt_mla_summary(self) -> str:
        return f"{self.mlt_fte:.0f} MLT and {self.mla_fte:.0f} MLA lines"


def status_label(status: ProspectStatus) -> str:
    return STATUS_LABELS.get(status, status.value.replace("_", " ").title())


def status_badge_class(status: ProspectStatus) -> str:
    if status == ProspectStatus.DISCOVERED:
        return "biz-badge-new"
    if status in {ProspectStatus.PREVIEWED, ProspectStatus.CONTACTED}:
        return "biz-badge-previewed"
    if status == ProspectStatus.ACTIVE_CLIENT:
        return "biz-badge-active"
    return "biz-badge-passed"


def icp_display_score(icp_score: int) -> tuple[int, int]:
    """Map backend 0–100 score to design-spec /25 display."""
    scaled = int(round(max(0, min(100, icp_score)) * 25 / 100))
    return scaled, 25


def icp_band(display_score: int) -> tuple[str, str]:
    if display_score >= 22:
        return "Strong fit", "revenue"
    if display_score >= 16:
        return "Good fit", "accent"
    if display_score >= 10:
        return "Moderate", "warning"
    return "Low fit", "muted"


def format_test_volume(volume: int) -> str:
    if volume >= 1_000_000:
        return f"{volume / 1_000_000:.1f}M tests/yr"
    if volume >= 1_000:
        return f"{volume / 1_000:.0f}k tests/yr"
    return f"{volume:,} tests/yr"


def format_volume_short(volume: int) -> str:
    if volume >= 1_000_000:
        return f"{volume / 1_000_000:.1f}M tests/yr"
    if volume >= 1_000:
        return f"{volume / 1_000:.0f}k tests/yr"
    return f"{volume:,}/yr"


def derive_pitch_angle(prospect: Prospect, enrichment: Optional[FacilityEnrichment]) -> str:
    if prospect.pain_signals:
        top = prospect.pain_signals[0]
        if "breakroom" in top.lower() or "excel" in top.lower():
            return f'Footer 2/2 before breakroom posting — {prospect.facility}'
        if "volume" in top.lower() or "ot" in top.lower():
            vol = format_volume_short(enrichment.annual_test_volume) if enrichment else "high volume"
            return f"Compliance-checked breakroom grid for {vol} at {prospect.facility}"
    if enrichment and enrichment.mlt_fte + enrichment.mla_fte >= 15:
        return (
            f"Your {enrichment.roster_summary} roster is a fit for our 8-week "
            f"Portage-style catalog — footer 2/2/2 before you publish."
        )
    return (
        f"Breakroom-ready scheduling for {prospect.facility} — "
        "preview before your next posting cycle."
    )


@lru_cache(maxsize=1)
def _facility_index() -> dict[str, RegionalFacilityRecord]:
    path = DEFAULT_FACILITY_DATASET
    if not path.is_file():
        return {}
    return {
        record.facility_id: record
        for record in load_regional_facility_dataset(path)
    }


def load_facility_enrichment(prospect: Prospect) -> Optional[FacilityEnrichment]:
    if not prospect.facility_id:
        return None
    record = _facility_index().get(prospect.facility_id)
    if record is None:
        return None
    report, _, _ = score_facility_record(record)
    return FacilityEnrichment(
        facility_id=record.facility_id,
        region=record.region,
        province=record.state_province,
        annual_test_volume=record.annual_test_volume,
        mlt_fte=record.mlt_fte,
        mla_fte=record.mla_fte,
        deployment_score=report.deployment_score,
        estimated_savings_usd=report.estimated_annual_savings_usd,
        rationale=report.rationale,
    )


def merge_template_variables(template: str, variables: Dict[str, str]) -> str:
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{{{key}}}}}", value)
    return result


def build_template_context(
    prospect: Prospect,
    enrichment: Optional[FacilityEnrichment],
    *,
    sender_name: str,
    pitch_angle: str,
    trial_link: str = "/?signup=1",
) -> dict[str, str]:
    first_name = ""
    if prospect.contact_name and prospect.contact_name.strip():
        first_name = prospect.contact_name.strip().split()[0]

    short_name = prospect.facility.replace(" Hospital", "").replace(" Regional Lab", "")
    pain_opener = (
        prospect.pain_signals[0]
        if prospect.pain_signals
        else (
            "Most managers I talk to still juggle separate tabs for evenings, nights, "
            "and a breakroom grid that has to match union rest rules."
        )
    )
    if prospect.pain_signals and (
        "test volume" in prospect.pain_signals[0].lower()
        or "ot" in prospect.pain_signals[0].lower()
    ):
        pain_opener = (
            f"{prospect.pain_signals[0].rstrip('.')}. "
            "That usually shows up as last-minute OT patches and equity questions mid-week."
        )
    savings = (
        f"${enrichment.estimated_savings_usd:,.0f}/yr"
        if enrichment
        else "significant annual savings"
    )
    return {
        "first_name": first_name or "there",
        "facility_name": prospect.facility,
        "facility_short_name": short_name,
        "region": enrichment.region if enrichment else "Manitoba",
        "mlt_mla_summary": enrichment.mlt_mla_summary if enrichment else "your MLT/MLA lines",
        "annual_test_volume": format_test_volume(enrichment.annual_test_volume) if enrichment else "your test volume",
        "pain_opener": pain_opener,
        "managed_offer_paragraph": (
            f"We run managed 8-week publishes for Manitoba hospital labs ({MANAGED_BLOCK_PRICE_LABEL}): "
            "roster lines and period dates in, compliance check and breakroom HTML out. "
            "You post the grid — we don't hand you another login to figure out solo."
        ),
        "solution_paragraph": (
            "We deliver an 8-week schedule that is legal, covered, and breakroom-ready — "
            "with Manitoba labor rules and vacant-line fairness built in."
        ),
        "proof_paragraph": (
            f"After a successful managed publish ({MANAGED_BLOCK_PRICE_LABEL}), teams can move to "
            f"Pro self-serve ({PRO_MONTHLY_PRICE_LABEL}) or explore with a free 14-day trial on "
            "a Portage-style demo roster."
        ),
        "estimated_savings": savings,
        "pitch_angle": pitch_angle,
        "cta_line": (
            'Reply with "yes — [week] works" and roughly how many MLT/MLA lines you run — '
            "I'll send walkthrough times."
        ),
        "sender_name": sender_name,
        "trial_link": trial_link,
    }


def email_preview_envelope_html(*, to: str, subject: str, body: str) -> str:
    """HTML for a mail-client-style preview (escaped, pre-wrapped body)."""

    recipient = to.strip() or "lab.manager@example.com"
    return textwrap.dedent(
        f"""
        <div class="biz-email-envelope">
          <div class="biz-email-meta">
            <span class="biz-email-label">To</span>
            <span>{html.escape(recipient)}</span>
          </div>
          <div class="biz-email-meta">
            <span class="biz-email-label">Subject</span>
            <span>{html.escape(subject)}</span>
          </div>
          <div class="biz-email-body">{html.escape(body)}</div>
        </div>
        """
    ).strip()


DEFAULT_EMAIL_BODY_TEMPLATE = """Hi {{first_name}},

Posting season at {{facility_name}} usually means evenings, nights, and the breakroom grid all have to line up — often from separate spreadsheets.

{{pain_opener}}

{{managed_offer_paragraph}}

{{cta_line}}

—
{{sender_name}}"""

DEFAULT_EMAIL_SUBJECT_TEMPLATE = "{{facility_name}} — breakroom grid before posting season?"

# persuasion-psychology-partner: FIRST_TOUCH_PSYCHOLOGY_BRIEF.md subject A/B/C
FIRST_TOUCH_SUBJECT_VARIANT_LABELS: dict[str, str] = {
    "A — breakroom grid (recommended)": "a",
    "B — posting season": "b",
    "C — quick question": "c",
}


def first_touch_subject(*, facility_name: str, variant: str = "a") -> str:
    """Return a psychology-brief subject line for variant a, b, or c."""
    templates = {
        "a": f"{facility_name} — breakroom grid before posting season?",
        "b": f"{facility_name} rotation — one question before you post",
        "c": f"Quick question — MLT lines at {facility_name}",
    }
    return templates.get(variant, templates["a"])


def blocked_honesty_phrases(text: str) -> List[str]:
    lowered = text.lower()
    return [phrase for phrase in HONESTY_BLOCKLIST if phrase in lowered]


def mailto_link(
    *,
    email: str,
    subject: str,
    body: str,
    reply_to: str | None = None,
) -> str:
    params: dict[str, str] = {"subject": subject, "body": body}
    if reply_to and reply_to.strip():
        params["reply-to"] = reply_to.strip()
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"mailto:{email}?{query}"


def inbound_reply_to_address() -> str | None:
    """Monitored inbox for Reply-To on outbound mailto (from env)."""

    import os

    explicit = os.environ.get("LAB_INBOUND_REPLY_TO", "").strip()
    if explicit:
        return explicit
    user = os.environ.get("LAB_INBOUND_IMAP_USER", "").strip()
    return user or None


def load_onboarding_tasks(conn: sqlite3.Connection, tenant_id: str) -> dict[str, bool]:
    from lab_scheduler.tenant.configuration import get_tenant_config_value

    raw = get_tenant_config_value(conn, tenant_id=tenant_id, config_key=ONBOARDING_CONFIG_KEY)
    if not raw:
        return {task_id: task_id == "create_tenant" for task_id, _ in DEFAULT_ONBOARDING_TASKS}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {task_id: task_id == "create_tenant" for task_id, _ in DEFAULT_ONBOARDING_TASKS}
    if not isinstance(parsed, dict):
        return {task_id: task_id == "create_tenant" for task_id, _ in DEFAULT_ONBOARDING_TASKS}
    base = {task_id: task_id == "create_tenant" for task_id, _ in DEFAULT_ONBOARDING_TASKS}
    for key, value in parsed.items():
        if key in base:
            base[key] = bool(value)
    return base


def save_onboarding_tasks(conn: sqlite3.Connection, tenant_id: str, tasks: dict[str, bool]) -> None:
    from lab_scheduler.tenant.configuration import set_tenant_config_value

    set_tenant_config_value(
        conn,
        tenant_id=tenant_id,
        config_key=ONBOARDING_CONFIG_KEY,
        config_value=json.dumps(tasks),
    )
    conn.commit()
