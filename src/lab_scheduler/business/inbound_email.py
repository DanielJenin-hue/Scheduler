"""Inbound email sync (IMAP) and manual logging for business prospects."""

from __future__ import annotations

import email
import imaplib
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header
from email.message import Message
from enum import Enum
from typing import List, Optional, Sequence

import sqlite3

from lab_scheduler.business.models import ProspectStatus, ensure_business_prospects_schema, utc_now_iso
from lab_scheduler.business.prospect_service import get_prospect, list_prospects, update_prospect_status

__all__ = [
    "ImapConfig",
    "InboundMessage",
    "InboundMessageStatus",
    "InboundServiceError",
    "ImapNotConfiguredError",
    "SyncResult",
    "active_conversation_count",
    "count_unread_inbound",
    "ensure_business_inbound_schema",
    "get_inbound_message",
    "list_inbound_messages",
    "log_manual_reply",
    "mark_inbound_archived",
    "mark_inbound_read",
    "match_prospect_for_inbound",
    "prospect_ids_with_inbound",
    "sync_inbound_from_imap",
]

_ENV_HOST = "LAB_INBOUND_IMAP_HOST"
_ENV_USER = "LAB_INBOUND_IMAP_USER"
_ENV_PASSWORD = "LAB_INBOUND_IMAP_PASSWORD"
_ENV_FOLDER = "LAB_INBOUND_IMAP_FOLDER"
_ENV_REPLY_TO = "LAB_INBOUND_REPLY_TO"


class InboundMessageStatus(str, Enum):
    UNREAD = "unread"
    READ = "read"
    ARCHIVED = "archived"

    @classmethod
    def normalize(cls, value: str | InboundMessageStatus) -> InboundMessageStatus:
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        return cls(text)


class InboundServiceError(ValueError):
    """Raised when an inbound workflow operation is invalid."""


class ImapNotConfiguredError(InboundServiceError):
    """Raised when required IMAP environment variables are missing."""


@dataclass(frozen=True, slots=True)
class ImapConfig:
    host: str
    user: str
    password: str
    folder: str = "INBOX"
    reply_to: str | None = None

    @classmethod
    def from_env(cls) -> ImapConfig:
        host = os.environ.get(_ENV_HOST, "").strip()
        user = os.environ.get(_ENV_USER, "").strip()
        password = os.environ.get(_ENV_PASSWORD, "").strip()
        if not host or not user or not password:
            raise ImapNotConfiguredError(
                f"Set {_ENV_HOST}, {_ENV_USER}, and {_ENV_PASSWORD} to sync inbound email."
            )
        folder = os.environ.get(_ENV_FOLDER, "INBOX").strip() or "INBOX"
        reply_to = os.environ.get(_ENV_REPLY_TO, user).strip() or user
        return cls(host=host, user=user, password=password, folder=folder, reply_to=reply_to)

    def is_configured(self) -> bool:
        return bool(self.host and self.user and self.password)


@dataclass(frozen=True, slots=True)
class SyncResult:
    fetched: int
    inserted: int
    matched: int
    skipped_duplicate: int


@dataclass(slots=True)
class InboundMessage:
    id: str
    from_email: str
    received_at: str
    prospect_id: Optional[str] = None
    to_email: Optional[str] = None
    subject: Optional[str] = None
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    read_at: Optional[str] = None
    thread_id: Optional[str] = None
    external_message_id: Optional[str] = None
    status: InboundMessageStatus = InboundMessageStatus.UNREAD
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict) -> InboundMessage:
        data = dict(row) if not isinstance(row, dict) else row
        return cls(
            id=str(data["id"]),
            prospect_id=data.get("prospect_id"),
            from_email=str(data["from_email"]),
            to_email=data.get("to_email"),
            subject=data.get("subject"),
            body_text=data.get("body_text"),
            body_html=data.get("body_html"),
            received_at=str(data["received_at"]),
            read_at=data.get("read_at"),
            thread_id=data.get("thread_id"),
            external_message_id=data.get("external_message_id"),
            status=InboundMessageStatus.normalize(str(data.get("status") or "unread")),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )


def ensure_business_inbound_schema(conn: sqlite3.Connection) -> None:
    """Create ``business_inbound_messages`` if missing (idempotent)."""

    ensure_business_prospects_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS business_inbound_messages (
          id                   TEXT PRIMARY KEY,
          prospect_id          TEXT REFERENCES business_prospects(id) ON DELETE SET NULL,
          from_email           TEXT NOT NULL,
          to_email             TEXT,
          subject              TEXT,
          body_text            TEXT,
          body_html            TEXT,
          received_at          TEXT NOT NULL,
          read_at              TEXT,
          thread_id            TEXT,
          external_message_id  TEXT,
          status               TEXT NOT NULL DEFAULT 'unread'
                                 CHECK (status IN ('unread', 'read', 'archived')),
          created_at           TEXT NOT NULL,
          updated_at           TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_business_inbound_external_message_id
          ON business_inbound_messages (external_message_id)
          WHERE external_message_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_inbound_prospect
          ON business_inbound_messages (prospect_id, received_at DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_inbound_status
          ON business_inbound_messages (status, received_at DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_inbound_thread
          ON business_inbound_messages (thread_id, received_at DESC)
        """
    )


def _decode_header_value(raw: str | None) -> str:
    if not raw:
        return ""
    parts: list[str] = []
    for fragment, charset in decode_header(raw):
        if isinstance(fragment, bytes):
            parts.append(fragment.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(str(fragment))
    return "".join(parts).strip()


def _extract_email_address(raw: str | None) -> str:
    text = _decode_header_value(raw)
    match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", text)
    return match.group(0).lower() if match else text.lower()


def _message_body_parts(msg: Message) -> tuple[str, str | None]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition.lower():
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if content_type == "text/plain":
                text_parts.append(decoded)
            elif content_type == "text/html":
                html_parts.append(decoded)
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_parts.append(decoded)
            else:
                text_parts.append(decoded)
    body_text = "\n".join(text_parts).strip() or None
    body_html = "\n".join(html_parts).strip() or None
    return body_text or "", body_html


def _normalize_subject(subject: str | None) -> str:
    text = (subject or "").strip()
    while text.lower().startswith("re:"):
        text = text[3:].strip()
    while text.lower().startswith("fwd:"):
        text = text[4:].strip()
    return text.lower()


def _subject_matches_draft(subject: str | None, draft_subject: str | None) -> bool:
    if not subject or not draft_subject:
        return False
    return _normalize_subject(subject) == _normalize_subject(draft_subject)


def match_prospect_for_inbound(
    conn: sqlite3.Connection,
    *,
    from_email: str,
    subject: str | None = None,
    in_reply_to: str | None = None,
) -> Optional[str]:
    """Match an inbound message to a prospect by sender, subject, or thread."""

    ensure_business_inbound_schema(conn)
    sender = from_email.strip().lower()
    if not sender:
        return None

    for prospect in list_prospects(conn):
        if prospect.email and prospect.email.strip().lower() == sender:
            return prospect.id
        if _subject_matches_draft(subject, prospect.email_draft_subject):
            return prospect.id
        if subject and prospect.facility:
            facility_lower = prospect.facility.lower()
            subj_lower = (subject or "").lower()
            if facility_lower in subj_lower:
                return prospect.id

    if in_reply_to:
        row = conn.execute(
            """
            SELECT prospect_id FROM business_inbound_messages
            WHERE external_message_id = ? AND prospect_id IS NOT NULL
            LIMIT 1
            """,
            (in_reply_to.strip(),),
        ).fetchone()
        if row and row[0]:
            return str(row[0])

    return None


def _bump_prospect_on_reply(conn: sqlite3.Connection, prospect_id: str) -> None:
    prospect = get_prospect(conn, prospect_id)
    if prospect.status in {ProspectStatus.ACTIVE_CLIENT, ProspectStatus.DECLINED}:
        return
    if prospect.status in {ProspectStatus.DISCOVERED, ProspectStatus.PREVIEWED}:
        update_prospect_status(conn, prospect_id, ProspectStatus.CONTACTED)


def _insert_inbound_message(
    conn: sqlite3.Connection,
    *,
    from_email: str,
    to_email: str | None,
    subject: str | None,
    body_text: str | None,
    body_html: str | None,
    received_at: str,
    thread_id: str | None,
    external_message_id: str | None,
    prospect_id: str | None,
) -> InboundMessage | None:
    ensure_business_inbound_schema(conn)
    if external_message_id:
        existing = conn.execute(
            "SELECT id FROM business_inbound_messages WHERE external_message_id = ?",
            (external_message_id,),
        ).fetchone()
        if existing is not None:
            return None

    now = utc_now_iso()
    message_id = f"inbound-{uuid.uuid4().hex[:12]}"
    conn.execute(
        """
        INSERT INTO business_inbound_messages (
          id, prospect_id, from_email, to_email, subject, body_text, body_html,
          received_at, read_at, thread_id, external_message_id, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 'unread', ?, ?)
        """,
        (
            message_id,
            prospect_id,
            from_email,
            to_email,
            subject,
            body_text,
            body_html,
            received_at,
            thread_id,
            external_message_id,
            now,
            now,
        ),
    )
    if prospect_id:
        _bump_prospect_on_reply(conn, prospect_id)
    conn.commit()
    return get_inbound_message(conn, message_id)


def get_inbound_message(conn: sqlite3.Connection, message_id: str) -> InboundMessage:
    ensure_business_inbound_schema(conn)
    row = conn.execute(
        "SELECT * FROM business_inbound_messages WHERE id = ?",
        (message_id,),
    ).fetchone()
    if row is None:
        raise InboundServiceError(f"Inbound message {message_id!r} not found.")
    return InboundMessage.from_row(row)


def list_inbound_messages(
    conn: sqlite3.Connection,
    *,
    status: InboundMessageStatus | str | None = None,
    prospect_id: str | None = None,
    limit: int | None = 50,
) -> List[InboundMessage]:
    ensure_business_inbound_schema(conn)
    clauses = ["1=1"]
    params: list[object] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(InboundMessageStatus.normalize(status).value)
    if prospect_id is not None:
        clauses.append("prospect_id = ?")
        params.append(prospect_id)
    sql = (
        "SELECT * FROM business_inbound_messages WHERE "
        + " AND ".join(clauses)
        + " ORDER BY received_at DESC"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [InboundMessage.from_row(row) for row in rows]


def count_unread_inbound(conn: sqlite3.Connection) -> int:
    ensure_business_inbound_schema(conn)
    row = conn.execute(
        "SELECT COUNT(*) FROM business_inbound_messages WHERE status = 'unread'",
    ).fetchone()
    return int(row[0]) if row else 0


def prospect_ids_with_inbound(conn: sqlite3.Connection) -> set[str]:
    ensure_business_inbound_schema(conn)
    rows = conn.execute(
        """
        SELECT DISTINCT prospect_id FROM business_inbound_messages
        WHERE prospect_id IS NOT NULL AND status != 'archived'
        """
    ).fetchall()
    return {str(row[0]) for row in rows if row[0]}


def active_conversation_count(conn: sqlite3.Connection) -> int:
    """Distinct prospects with unread inbound or recent reply activity."""

    ensure_business_inbound_schema(conn)
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT prospect_id) FROM business_inbound_messages
        WHERE prospect_id IS NOT NULL
          AND status IN ('unread', 'read')
          AND status != 'archived'
        """
    ).fetchone()
    return int(row[0]) if row else 0


def mark_inbound_read(conn: sqlite3.Connection, message_id: str) -> InboundMessage:
    ensure_business_inbound_schema(conn)
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE business_inbound_messages
        SET status = 'read', read_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (now, now, message_id),
    )
    conn.commit()
    return get_inbound_message(conn, message_id)


def mark_inbound_archived(conn: sqlite3.Connection, message_id: str) -> InboundMessage:
    ensure_business_inbound_schema(conn)
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE business_inbound_messages
        SET status = 'archived', updated_at = ?
        WHERE id = ?
        """,
        (now, message_id),
    )
    conn.commit()
    return get_inbound_message(conn, message_id)


def log_manual_reply(
    conn: sqlite3.Connection,
    *,
    from_email: str,
    subject: str,
    body_text: str,
    prospect_id: str | None = None,
    received_at: str | None = None,
) -> InboundMessage:
    """Manually log a reply when IMAP is not configured."""

    if not from_email.strip():
        raise InboundServiceError("from_email is required.")
    if not body_text.strip():
        raise InboundServiceError("body_text is required.")

    resolved_prospect = prospect_id
    if resolved_prospect is None:
        resolved_prospect = match_prospect_for_inbound(
            conn,
            from_email=from_email,
            subject=subject,
        )

    created = _insert_inbound_message(
        conn,
        from_email=from_email.strip().lower(),
        to_email=None,
        subject=subject.strip() or None,
        body_text=body_text.strip(),
        body_html=None,
        received_at=received_at or utc_now_iso(),
        thread_id=None,
        external_message_id=None,
        prospect_id=resolved_prospect,
    )
    if created is None:
        raise InboundServiceError("Failed to log manual reply.")
    return created


def _parse_imap_message(raw_bytes: bytes) -> tuple[InboundMessage | None, bool]:
    msg = email.message_from_bytes(raw_bytes)
    external_id = (msg.get("Message-ID") or "").strip() or None
    from_email = _extract_email_address(msg.get("From"))
    to_email = _extract_email_address(msg.get("To")) or None
    subject = _decode_header_value(msg.get("Subject")) or None
    in_reply_to = (msg.get("In-Reply-To") or msg.get("References") or "").strip() or None
    thread_id = in_reply_to or external_id
    date_header = msg.get("Date")
    received_at = utc_now_iso()
    if date_header:
        try:
            parsed = email.utils.parsedate_to_datetime(date_header)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            received_at = parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError, OverflowError):
            pass
    body_text, body_html = _message_body_parts(msg)
    return (
        InboundMessage(
            id="",
            from_email=from_email,
            to_email=to_email,
            subject=subject,
            body_text=body_text or None,
            body_html=body_html,
            received_at=received_at,
            thread_id=thread_id,
            external_message_id=external_id,
        ),
        external_id is not None,
    )


def sync_inbound_from_imap(
    conn: sqlite3.Connection,
    *,
    config: ImapConfig | None = None,
    max_messages: int = 50,
) -> SyncResult:
    """Fetch new messages from IMAP and store unmatched duplicates safely."""

    cfg = config or ImapConfig.from_env()
    ensure_business_inbound_schema(conn)

    inserted = 0
    matched = 0
    skipped_duplicate = 0
    fetched = 0

    mail = imaplib.IMAP4_SSL(cfg.host)
    try:
        mail.login(cfg.user, cfg.password)
        status, _ = mail.select(cfg.folder, readonly=True)
        if status != "OK":
            raise InboundServiceError(f"Could not open IMAP folder {cfg.folder!r}.")

        status, data = mail.search(None, "UNSEEN")
        if status != "OK":
            raise InboundServiceError("IMAP search failed.")
        ids = (data[0] or b"").split()
        if not ids:
            status, data = mail.search(None, "ALL")
            if status != "OK":
                raise InboundServiceError("IMAP search failed.")
            ids = (data[0] or b"").split()[-max_messages:]

        for msg_id in reversed(ids[-max_messages:]):
            fetched += 1
            status, payload = mail.fetch(msg_id, "(RFC822)")
            if status != "OK" or not payload or not payload[0]:
                continue
            raw = payload[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            parsed, _ = _parse_imap_message(bytes(raw))
            if not parsed.from_email:
                continue
            prospect_id = match_prospect_for_inbound(
                conn,
                from_email=parsed.from_email,
                subject=parsed.subject,
                in_reply_to=parsed.thread_id,
            )
            created = _insert_inbound_message(
                conn,
                from_email=parsed.from_email,
                to_email=parsed.to_email,
                subject=parsed.subject,
                body_text=parsed.body_text,
                body_html=parsed.body_html,
                received_at=parsed.received_at,
                thread_id=parsed.thread_id,
                external_message_id=parsed.external_message_id,
                prospect_id=prospect_id,
            )
            if created is None:
                skipped_duplicate += 1
            else:
                inserted += 1
                if prospect_id:
                    matched += 1
    finally:
        try:
            mail.logout()
        except imaplib.IMAP4.error:
            pass

    return SyncResult(
        fetched=fetched,
        inserted=inserted,
        matched=matched,
        skipped_duplicate=skipped_duplicate,
    )


def imap_setup_instructions() -> str:
    """Human-readable setup steps for the Inbox empty state."""

    return (
        "Set environment variables before starting the app:\n\n"
        f"- `{_ENV_HOST}` — e.g. `imap.gmail.com` or `outlook.office365.com`\n"
        f"- `{_ENV_USER}` — your monitored inbox address\n"
        f"- `{_ENV_PASSWORD}` — app password (not your login password)\n"
        f"- `{_ENV_FOLDER}` — optional, default `INBOX`\n"
        f"- `{_ENV_REPLY_TO}` — optional, shown in outbound mailto (defaults to `{_ENV_USER}`)\n\n"
        "**Gmail:** Google Account → Security → 2-Step Verification → App passwords → "
        "create one for Mail. Use `imap.gmail.com` and enable IMAP in Gmail settings.\n\n"
        "**Outlook / Microsoft 365:** Use `outlook.office365.com`, enable IMAP in Outlook "
        "settings, and create an app password if MFA is on.\n\n"
        "Until IMAP is configured, use **Log reply manually** below."
    )
