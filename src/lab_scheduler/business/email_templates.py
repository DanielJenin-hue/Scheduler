"""Personalized outreach email drafts for business prospects."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from lab_scheduler.business.models import Prospect

__all__ = [
    "EmailDraft",
    "FIRST_TOUCH_JARGON_GLOSSARY",
    "MANAGED_BLOCK_PRICE_LABEL",
    "OUTBOUND_REPLY_TO_NOTES",
    "PRODUCT_VALUE_PROPS",
    "PRO_MONTHLY_PRICE_LABEL",
    "default_outreach_sender_name",
    "generate_outreach_email",
    "format_pain_signals_for_email",
    "first_touch_subject_variants",
    "managed_offer_paragraph",
    "translate_pain_signal_for_email",
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

# Manager-native glossary — internal/product terms → plain language for cold hospital managers.
# Used by validate_first_touch_draft (warn when banned phrase lacks its gloss in the body).
FIRST_TOUCH_JARGON_GLOSSARY: tuple[tuple[str, str], ...] = (
    ("breakroom html", "print and post"),
    ("breakroom-ready html", "print and post"),
    ("html export", "print and post"),
    ("breakroom grid", "schedule on the wall"),
    ("breakroom-ready", "ready to print and post"),
    ("breakroom posting", "posting on the wall"),
    ("managed 8-week publish", "8-week rotation from your roster"),
    ("managed publish", "rotation from your roster"),
    ("compliance check", "manitoba rest rules"),
    ("audit-ready schedules", "manitoba rest rules"),
    ("rsi pass", "rest rules"),
    ("distribute/fill/save", "roster lines"),
    ("distribute fill save", "roster lines"),
    ("port optical", "lab scheduling"),
)

# Value propositions aligned with deploy/landing.html — manager-facing wording.
PRODUCT_VALUE_PROPS: tuple[str, ...] = (
    "Build an 8-week lab schedule that is covered, fair across lines, and ready to post on the wall",
    "Fill M/E/N master rotations with Manitoba rest rules and vacant-line fairness",
    "Hand managers a print-ready schedule — not another Excel weekend",
    "Full roster support for 15–60 MLT/MLA lines with union fatigue rules",
    "Rest-rule review before you post the schedule staff see Monday morning",
)

_FIRST_TOUCH_CTA = (
    'Reply with "yes — [week] works" and roughly how many MLT/MLA lines you run — '
    "I'll send walkthrough times."
)

# Generic Manitoba compliance line from discovery — weave into opener, not a standalone bullet.
_GENERIC_COMPLIANCE_PHRASE = "manitoba union fatigue and rest rules require audit-ready schedules"

_PAIN_PRIORITY_KEYWORDS = (
    "excel",
    "wall",
    "posting",
    "spreadsheet",
    "volume",
    " ot",
    "overtime",
    "roster",
    "rotation",
    "footer",
    "coverage",
    "equity",
    "savings",
    "weekend",
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
        "a": f"{name} — staff schedule before posting season?",
        "b": f"{name} rotation — one question before you post",
        "c": f"Quick question — MLT lines at {name}",
    }


def translate_pain_signal_for_email(signal: str) -> str:
    """Rewrite discovery pain_signals into manager-native language for email weaving."""
    lowered = signal.lower()
    if "breakroom-ready html" in lowered or "breakroom html" in lowered:
        return (
            "Posting season still means weekends in Excel before staff see the schedule on the wall"
        )
    if "excel-based breakroom" in lowered or "breakroom posting" in lowered:
        return "Growing roster outpaces keeping a wall-ready schedule in Excel"
    if _GENERIC_COMPLIANCE_PHRASE in lowered:
        return signal
    if "audit-ready schedules" in lowered:
        return "Manitoba rest rules (consecutive nights, weekends) need to be right before you post"
    return signal


def _pick_primary_pain_signal(signals: Sequence[str]) -> Optional[str]:
    if not signals:
        return None
    translated = [translate_pain_signal_for_email(s).strip() for s in signals]
    for keyword in _PAIN_PRIORITY_KEYWORDS:
        for signal in translated:
            if keyword in signal.lower():
                return signal
    for signal in translated:
        if _GENERIC_COMPLIANCE_PHRASE not in signal.lower():
            return signal
    return translated[0]


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
    if "excel" in lowered or "wall" in lowered or "posting" in lowered or "weekend" in lowered:
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
        f"Posting season at {facility} usually means M/E/N coverage, footer gaps, "
        "and last-minute Excel before staff see the schedule."
    )
    if not pain_mirror:
        return base
    if _GENERIC_COMPLIANCE_PHRASE in pain_mirror.lower():
        return base
    pain_lower = pain_mirror.lower()
    if any(token in pain_lower for token in ("excel", "wall", "posting", "weekend", "spreadsheet")):
        return base
    return f"{base} {pain_mirror}"


def managed_offer_paragraph(*, include_pricing: bool = False) -> str:
    """Managed-first offer line — price deferred by default (see FIRST_TOUCH_PSYCHOLOGY_BRIEF)."""
    offer_core = (
        "We build your 8-week rotation from your MLT/MLA lines, check Manitoba rest rules, "
        "and send a schedule you can print and post on the wall (or share as a link). "
        "You review, then post."
    )
    if include_pricing:
        return (
            f"{offer_core} Typically {MANAGED_BLOCK_PRICE_LABEL} depending on roster size — "
            "exact scope and fee after a 15-minute walkthrough."
        )
    return (
        f"{offer_core} Fixed fee once we confirm line count on a short walkthrough."
    )


def generate_outreach_email(
    prospect: Prospect,
    *,
    sender_name: str | None = None,
    extra_context: Optional[str] = None,
    subject_variant: str = "a",
    include_pricing: bool = False,
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
        managed_offer_paragraph(include_pricing=include_pricing),
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

    for banned, gloss in FIRST_TOUCH_JARGON_GLOSSARY:
        if banned in lowered_combined and gloss not in lowered_body:
            warnings.append(
                f'Product jargon "{banned}" — managers expect plain language like "{gloss}"'
            )

    if re.search(r"\brsi\b", lowered_combined) and "rest rules" not in lowered_body:
        warnings.append('Internal jargon "RSI" — omit or say "Manitoba rest rules"')

    if "pro self-serve" in lowered_body or "sample breakroom" in lowered_body:
        warnings.append("Deferred offer (Pro/trial/sample) in first touch — save for follow-up #2")

    if re.search(r"\$[\d,]+", body):
        warnings.append(
            "Dollar amount in first touch — defer pricing to discovery call unless they asked about budget"
        )

    return warnings
