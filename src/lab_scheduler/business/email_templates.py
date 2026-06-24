"""Personalized outreach email drafts for business prospects."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from lab_scheduler.business.models import Prospect

__all__ = [
    "EmailDraft",
    "MANAGED_BLOCK_PRICE_LABEL",
    "OUTBOUND_REPLY_TO_NOTES",
    "PRODUCT_VALUE_PROPS",
    "PRO_MONTHLY_PRICE_LABEL",
    "default_outreach_sender_name",
    "generate_outreach_email",
    "format_pain_signals_for_email",
    "first_touch_subject_variants",
    "validate_first_touch_draft",
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

_FIRST_TOUCH_CTA = (
    'Reply with "yes — [week] works" and roughly how many MLT/MLA lines you run — '
    "I'll send walkthrough times."
)

# Generic Manitoba compliance line from discovery — weave into opener, not a standalone bullet.
_GENERIC_COMPLIANCE_PHRASE = "manitoba union fatigue and rest rules require audit-ready schedules"

_PAIN_PRIORITY_KEYWORDS = (
    "breakroom",
    "excel",
    "volume",
    " ot",
    "overtime",
    "roster",
    "rotation",
    "footer",
    "posting",
    "coverage",
    "equity",
    "savings",
)


@dataclass(frozen=True, slots=True)
class EmailDraft:
    subject: str
    body: str


def default_outreach_sender_name() -> str:
    """Peer sign-off for first-touch mail — override via LAB_OUTREACH_SENDER_NAME."""
    return os.environ.get("LAB_OUTREACH_SENDER_NAME", "Dan").strip() or "Dan"


def first_touch_subject_variants(facility: str) -> dict[str, str]:
    """Psychology-brief subject A/B/C (see docs/FIRST_TOUCH_PSYCHOLOGY_BRIEF.md)."""
    name = facility.strip()
    return {
        "a": f"{name} — breakroom grid before posting season?",
        "b": f"{name} rotation — one question before you post",
        "c": f"Quick question — MLT lines at {name}",
    }


def _pick_primary_pain_signal(signals: Sequence[str]) -> Optional[str]:
    if not signals:
        return None
    for keyword in _PAIN_PRIORITY_KEYWORDS:
        for signal in signals:
            if keyword in signal.lower():
                return signal.strip()
    for signal in signals:
        if _GENERIC_COMPLIANCE_PHRASE not in signal.lower():
            return signal.strip()
    return signals[0].strip()


def format_pain_signals_for_email(signals: Sequence[str], *, max_items: int = 3) -> str:
    """Return one peer-style pain mirror sentence — no bullet stacks."""
    primary = _pick_primary_pain_signal(signals)
    if not primary:
        return ""
    lowered = primary.lower()
    if "test volume" in lowered or " ot" in lowered or "overtime" in lowered:
        return (
            f"{primary.rstrip('.')}. "
            "That usually shows up as last-minute OT patches and equity questions mid-week."
        )
    if "breakroom" in lowered or "excel" in lowered:
        return primary.rstrip(".") + "."
    if _GENERIC_COMPLIANCE_PHRASE in lowered:
        return ""
    return primary.rstrip(".") + "."


def _greeting(contact_name: Optional[str], facility: str) -> str:
    if contact_name and contact_name.strip():
        first = contact_name.strip().split()[0]
        return f"Hi {first},"
    short = facility.replace(" Health Centre", "").replace(" Hospital", "").strip()
    return f"Hi — quick note for whoever runs lab scheduling at {short}:"


def _facility_opener(facility: str, pain_mirror: str) -> str:
    base = (
        f"Posting season at {facility} usually means evenings, nights, and the breakroom grid "
        "have to line up before staff see it — often still built across separate spreadsheets."
    )
    if not pain_mirror:
        return base
    if _GENERIC_COMPLIANCE_PHRASE in pain_mirror.lower():
        return base
    return f"{base} {pain_mirror}"


def _managed_offer_line() -> str:
    return (
        f"We run managed 8-week publishes for Manitoba hospital labs ({MANAGED_BLOCK_PRICE_LABEL}): "
        "roster and period dates in, compliance check and breakroom HTML out. "
        "You post the grid — we don't hand you another login to figure out solo."
    )


def generate_outreach_email(
    prospect: Prospect,
    *,
    sender_name: str | None = None,
    extra_context: Optional[str] = None,
    subject_variant: str = "a",
) -> EmailDraft:
    """Generate a managed-first, single-CTA outreach email (~90 words) for preview before sending."""

    resolved_sender = (sender_name or default_outreach_sender_name()).strip()
    greeting = _greeting(prospect.contact_name, prospect.facility)
    facility = prospect.facility.strip()
    subjects = first_touch_subject_variants(facility)
    subject = subjects.get(subject_variant.lower(), subjects["a"])
    pain_mirror = format_pain_signals_for_email(prospect.pain_signals)
    opener = _facility_opener(facility, pain_mirror)

    body_parts = [
        greeting,
        "",
        opener,
        "",
        _managed_offer_line(),
    ]

    if extra_context and extra_context.strip():
        body_parts.extend(["", extra_context.strip()])

    body_parts.extend(
        [
            "",
            _FIRST_TOUCH_CTA,
            "",
            "—",
            resolved_sender,
        ]
    )

    return EmailDraft(subject=subject, body="\n".join(body_parts))


def validate_first_touch_draft(body: str, subject: str = "") -> list[str]:
    """Runtime quality gate for first-touch drafts — returns human-readable warnings."""
    warnings: list[str] = []
    combined = f"{subject}\n{body}"
    lowered_body = body.lower()
    lowered_combined = combined.lower()

    if re.search(r"\bportage\b", lowered_body) and "portage-style" not in lowered_body:
        warnings.append("Sign-off or body mentions Portage — use LAB_OUTREACH_SENDER_NAME for peer tone")

    if "example.com" in lowered_combined:
        warnings.append("Placeholder example.com address — add a real recipient before sending")

    bullet_lines = [
        line
        for line in body.splitlines()
        if line.strip().startswith(("-", "•", "*")) and len(line.strip()) > 2
    ]
    if len(bullet_lines) >= 2:
        warnings.append("Bullet stack detected — first touch should be prose paragraphs only")
    elif len(bullet_lines) == 1:
        warnings.append("Single bullet line — convert to a peer sentence per psychology brief")

    cta_patterns = (
        r'reply with\s+"yes',
        r"book a\s+\d+",
        r"calendar",
        r"14-day trial",
        r"start here:",
        r"click here",
        r"schedule a call",
    )
    cta_hits = sum(1 for pattern in cta_patterns if re.search(pattern, lowered_body))
    if cta_hits > 1:
        warnings.append("Multiple CTAs — keep one low-commitment reply ask in first touch")

    if re.match(r"^hello,\s*$", body.strip().splitlines()[0].lower() if body.strip() else ""):
        warnings.append('Generic "Hello," opener — use first name or facility-specific greeting')

    jargon = ("north star", "$2,000 mrr", "mrr cockpit", "hipaa certified", "trusted by")
    for phrase in jargon:
        if phrase in lowered_combined:
            warnings.append(f'Internal jargon "{phrase}" — remove before sending to prospects')

    if "pro self-serve" in lowered_body or "sample breakroom" in lowered_body:
        warnings.append("Deferred offer (Pro/trial/sample) in first touch — save for follow-up #2")

    return warnings
