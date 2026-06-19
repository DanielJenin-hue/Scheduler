"""Tests for inbound email sync and manual logging."""

from __future__ import annotations

import sqlite3

import pytest

from lab_scheduler.business.inbound_email import (
    ImapConfig,
    ImapNotConfiguredError,
    InboundMessageStatus,
    active_conversation_count,
    count_unread_inbound,
    ensure_business_inbound_schema,
    list_inbound_messages,
    log_manual_reply,
    mark_inbound_archived,
    mark_inbound_read,
    match_prospect_for_inbound,
    prospect_ids_with_inbound,
    sync_inbound_from_imap,
)
from lab_scheduler.business.models import ProspectStatus, ensure_business_prospects_schema
from lab_scheduler.business.prospect_service import create_prospect, generate_email_preview, get_prospect
from lab_scheduler.ui.business.helpers import inbound_reply_to_address, mailto_link


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_business_prospects_schema(conn)
    ensure_business_inbound_schema(conn)
    return conn


def test_ensure_business_inbound_schema_idempotent() -> None:
    conn = _memory_db()
    ensure_business_inbound_schema(conn)
    ensure_business_inbound_schema(conn)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='business_inbound_messages'"
    ).fetchone()
    assert row is not None


def test_match_prospect_by_sender_email() -> None:
    conn = _memory_db()
    created = create_prospect(
        conn,
        facility="St. Boniface Hospital",
        email="lab.manager@stboniface.ca",
    )
    matched = match_prospect_for_inbound(
        conn,
        from_email="lab.manager@stboniface.ca",
        subject="Re: Breakroom scheduling",
    )
    assert matched == created.id


def test_match_prospect_by_draft_subject() -> None:
    conn = _memory_db()
    created = create_prospect(conn, facility="Brandon Regional Lab")
    generate_email_preview(conn, created.id, mark_previewed=True)
    prospect = get_prospect(conn, created.id)
    matched = match_prospect_for_inbound(
        conn,
        from_email="unknown@example.com",
        subject=f"Re: {prospect.email_draft_subject}",
    )
    assert matched == created.id


def test_log_manual_reply_bumps_prospect_to_contacted() -> None:
    conn = _memory_db()
    created = create_prospect(
        conn,
        facility="Selkirk Regional Lab",
        email="manager@selkirk.ca",
    )
    generate_email_preview(conn, created.id, mark_previewed=True)
    message = log_manual_reply(
        conn,
        from_email="manager@selkirk.ca",
        subject="Re: Scheduling idea",
        body_text="Yes, let's talk next week.",
    )
    assert message.prospect_id == created.id
    assert message.status == InboundMessageStatus.UNREAD
    refreshed = get_prospect(conn, created.id)
    assert refreshed.status == ProspectStatus.CONTACTED


def test_mark_read_and_archive() -> None:
    conn = _memory_db()
    create_prospect(conn, facility="Test Lab", email="a@test.com")
    message = log_manual_reply(
        conn,
        from_email="a@test.com",
        subject="Hello",
        body_text="Interested.",
    )
    read = mark_inbound_read(conn, message.id)
    assert read.status == InboundMessageStatus.READ
    assert read.read_at
    archived = mark_inbound_archived(conn, message.id)
    assert archived.status == InboundMessageStatus.ARCHIVED


def test_count_unread_and_active_conversations() -> None:
    conn = _memory_db()
    create_prospect(conn, facility="Lab A", email="a@test.com")
    create_prospect(conn, facility="Lab B", email="b@test.com")
    log_manual_reply(conn, from_email="a@test.com", subject="A", body_text="One")
    log_manual_reply(conn, from_email="b@test.com", subject="B", body_text="Two")
    assert count_unread_inbound(conn) == 2
    assert active_conversation_count(conn) == 2
    messages = list_inbound_messages(conn)
    mark_inbound_read(conn, messages[0].id)
    assert count_unread_inbound(conn) == 1


def test_prospect_ids_with_inbound_excludes_archived() -> None:
    conn = _memory_db()
    create_prospect(conn, facility="Lab A", email="a@test.com")
    message = log_manual_reply(
        conn,
        from_email="a@test.com",
        subject="Hi",
        body_text="Reply",
    )
    assert prospect_ids_with_inbound(conn)
    mark_inbound_archived(conn, message.id)
    assert not prospect_ids_with_inbound(conn)


def test_imap_config_from_env_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAB_INBOUND_IMAP_HOST", raising=False)
    monkeypatch.delenv("LAB_INBOUND_IMAP_USER", raising=False)
    monkeypatch.delenv("LAB_INBOUND_IMAP_PASSWORD", raising=False)
    with pytest.raises(ImapNotConfiguredError):
        ImapConfig.from_env()


def test_imap_config_from_env_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAB_INBOUND_IMAP_HOST", "imap.gmail.com")
    monkeypatch.setenv("LAB_INBOUND_IMAP_USER", "inbox@example.com")
    monkeypatch.setenv("LAB_INBOUND_IMAP_PASSWORD", "secret")
    cfg = ImapConfig.from_env()
    assert cfg.host == "imap.gmail.com"
    assert cfg.reply_to == "inbox@example.com"


def test_sync_inbound_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _memory_db()
    create_prospect(conn, facility="Lab", email="prospect@test.com")

    class FakeMail:
        def __init__(self, host: str) -> None:
            self.host = host

        def login(self, user: str, password: str) -> tuple[str, list]:
            return "OK", [b"Logged in"]

        def select(self, folder: str, readonly: bool = True) -> tuple[str, list]:
            return "OK", [b"1"]

        def search(self, charset: str | None, *criteria: str) -> tuple[str, list]:
            return "OK", [b""]

        def fetch(self, msg_id: bytes, query: str) -> tuple[str, list]:
            return "OK", [None]

        def logout(self) -> tuple[str, list]:
            return "OK", [b"Bye"]

    monkeypatch.setattr("lab_scheduler.business.inbound_email.imaplib.IMAP4_SSL", FakeMail)
    cfg = ImapConfig(host="imap.test.com", user="u", password="p")
    result = sync_inbound_from_imap(conn, config=cfg)
    assert result.fetched == 0
    assert result.inserted == 0


def test_mailto_includes_reply_to() -> None:
    link = mailto_link(
        email="prospect@lab.ca",
        subject="Hello",
        body="Body",
        reply_to="inbox@mycompany.ca",
    )
    assert "reply-to=inbox%40mycompany.ca" in link


def test_inbound_reply_to_address_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAB_INBOUND_REPLY_TO", "replies@example.com")
    assert inbound_reply_to_address() == "replies@example.com"
