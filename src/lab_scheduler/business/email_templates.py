"""Personalized outreach email drafts for business prospects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from lab_scheduler.business.models import Prospect

__all__ = [
    "EmailDraft",
    "MANAGED_BLOCK_PRICE_LABEL",
    "OUTBOUND_REPLY_TO_NOTES",
    "PRODUCT_VALUE_PROPS",
    "PRO_MONTHLY_PRICE_LABEL",
    "generate_outreach_email",
    "format_pain_signals_for_email",
    "first_touch_subject_variants",
]

# Managed-first GTM (REVENUE_2000_PLAN) — trial/Pro are footnotes, not the lead hook.
MANAGED_BLOCK_PRICE_LABEL = "$800–1,200 CAD"
PRO_MONTHLY_PRICE_LABEL = "$299 CAD/month"

# Shown in Email Preview UI — set Reply-To so prospect replies land in Business Inbox.
OUTBOUND_REPLY_TO_NOTES = (
    "Set Reply-To to your monitored inbox (LAB_INBOUND_IMAP_USER or LAB_INBOUND_REPLY_TO) "
    "so replies sync into Business → Inbox. Gmail/Outlook: use an app password for IMAP sync."
)

# Value propositions aligned with deploy/landing.html
PRODUCT_VALUE_PROPS: tuple[str, ...] = (
    "Build an 8-week lab schedule that is legal, covered, and breakroom-ready",
    "Fill M/E/N master rotations with Manitoba labor rules and vacant-line fairness",
    "Export breakroom HTML managers can post today — not another Excel weekend",
    "Full roster support for 15–60 MLT/MLA lines with union fatigue rules",
    "Compliance audit trail before you publish the breakroom grid",
)


@dataclass(frozen=True, slots=True)
class EmailDraft:
    subject: str
    body: str


def first_touch_subject_variants(facility: str) -> dict[str, str]:
    """Psychology-brief subject A/B/C (see docs/FIRST_TOUCH_PSYCHOLOGY_BRIEF.md)."""
    name = facility.strip()
    return {
        "a": f"{name} — breakroom grid before posting season?",
        "b": f"{name} rotation — one question before you post",
        "c": f"Quick question — MLT lines at {name}",
    }


def format_pain_signals_for_email(signals: Sequence[str], *, max_items: int = 3) -> str:
    if not signals:
        return (
            "Most managers I talk to still juggle separate tabs for evenings, nights, "
            "and a breakroom grid that has to match union rest rules."
        )
    primary = signals[0].strip()
    if "test volume" in primary.lower() or "ot" in primary.lower():
        return (
            f"{primary.rstrip('.')}. "
            "That usually shows up as last-minute OT patches and equity questions mid-week."
        )
    if len(signals) == 1:
        return primary
    return primary


def _greeting(contact_name: Optional[str]) -> str:
    if contact_name and contact_name.strip():
        first = contact_name.strip().split()[0]
        return f"Hi {first},"
    return "Hello,"


def generate_outreach_email(
    prospect: Prospect,
    *,
    sender_name: str = "Port Optical team",
    extra_context: Optional[str] = None,
    subject_variant: str = "a",
) -> EmailDraft:
    """Generate a managed-first, single-CTA outreach email for preview before sending."""

    greeting = _greeting(prospect.contact_name)
    facility = prospect.facility.strip()
    subjects = first_touch_subject_variants(facility)
    subject = subjects.get(subject_variant.lower(), subjects["a"])
    pain_mirror = format_pain_signals_for_email(prospect.pain_signals)

    body_parts = [
        greeting,
        "",
        f"Posting season at {facility} usually means evenings, nights, and the breakroom grid "
        "all have to line up — often from separate spreadsheets.",
        "",
        pain_mirror,
        "",
        f"We run managed 8-week publishes for Manitoba hospital labs ({MANAGED_BLOCK_PRICE_LABEL}): "
        "roster lines and period dates in, compliance check and breakroom HTML out. "
        "You post the grid — we don't hand you another login to figure out solo.",
    ]

    if extra_context and extra_context.strip():
        body_parts.extend(["", extra_context.strip()])

    body_parts.extend(
        [
            "",
            'Reply with "yes — [week] works" and roughly how many MLT/MLA lines you run — '
            "I'll send walkthrough times.",
            "",
            "—",
            sender_name,
        ]
    )

    return EmailDraft(subject=subject, body="\n".join(body_parts))
