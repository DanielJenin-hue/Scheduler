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


def format_pain_signals_for_email(signals: Sequence[str], *, max_items: int = 3) -> str:
    if not signals:
        return (
            "Many hospital lab managers tell us breakroom posting still depends on "
            "manual spreadsheets and last-minute OT patches."
        )
    selected = list(signals[:max_items])
    if len(selected) == 1:
        return selected[0]
    bullets = "\n".join(f"  • {signal}" for signal in selected)
    return f"From what we see at similar Manitoba labs:\n{bullets}"


def _greeting(contact_name: Optional[str]) -> str:
    if contact_name and contact_name.strip():
        first = contact_name.strip().split()[0]
        return f"Hi {first},"
    return "Hello,"


def _pick_value_props(pain_signals: Sequence[str], *, count: int = 2) -> list[str]:
    props = list(PRODUCT_VALUE_PROPS)
    lowered = " ".join(pain_signals).lower()
    prioritized: list[str] = []
    if "breakroom" in lowered or "excel" in lowered:
        prioritized.append(PRODUCT_VALUE_PROPS[2])
    if "union" in lowered or "fatigue" in lowered or "manitoba" in lowered:
        prioritized.append(PRODUCT_VALUE_PROPS[1])
    if "volume" in lowered or "ot" in lowered or "coverage" in lowered:
        prioritized.append(PRODUCT_VALUE_PROPS[0])
    if "roster" in lowered or "fairness" in lowered:
        prioritized.append(PRODUCT_VALUE_PROPS[3])
    if "audit" in lowered or "compliance" in lowered:
        prioritized.append(PRODUCT_VALUE_PROPS[4])

    seen: set[str] = set()
    ordered: list[str] = []
    for prop in prioritized + props:
        if prop not in seen:
            seen.add(prop)
            ordered.append(prop)
        if len(ordered) >= count:
            break
    return ordered


def generate_outreach_email(
    prospect: Prospect,
    *,
    sender_name: str = "Port Optical team",
    extra_context: Optional[str] = None,
) -> EmailDraft:
    """Generate a professional, non-spammy outreach email for preview before sending."""

    greeting = _greeting(prospect.contact_name)
    facility = prospect.facility.strip()
    pain_block = format_pain_signals_for_email(prospect.pain_signals)

    subject = f"{facility} — breakroom grid ready for a quick look?"

    body_parts = [
        greeting,
        "",
        "I work with Manitoba hospital labs on breakroom-ready rotation schedules — "
        "evening/night coverage, union rest rules, and the posted grid all have to line up.",
        "",
        f"{facility} is the kind of roster where that alignment really matters.",
        "",
        pain_block,
        "",
        f"Most labs we work with start with a managed 8-week publish ({MANAGED_BLOCK_PRICE_LABEL}): "
        "you send roster lines and period dates, we build the schedule, run a compliance check, "
        "and deliver breakroom HTML you can post.",
    ]

    if extra_context and extra_context.strip():
        body_parts.extend(["", extra_context.strip()])

    body_parts.extend(
        [
            "",
            'Reply with "yes — [week] works" and roughly how many MLT/MLA lines you run — '
            "I'll follow up with times for a 15-minute walkthrough.",
            "",
            "—",
            sender_name,
        ]
    )

    return EmailDraft(subject=subject, body="\n".join(body_parts))
