import sqlite3



from lab_scheduler.telemetry.sentry_watcher import (

    ensure_sentry_schema,

    extract_exception_origin,

    fetch_sentry_logs,

    generate_llm_diagnostic_packet,

    log_unhandled_exception,

    update_sentry_log_for_review,

)





def _memory_db() -> sqlite3.Connection:

    conn = sqlite3.connect(":memory:")

    ensure_sentry_schema(conn)

    return conn





def test_log_and_fetch_unresolved_exception() -> None:

    conn = _memory_db()

    log_id = log_unhandled_exception(

        conn,

        RuntimeError("scheduler engine failure"),

        tenant_id="tenant-a",

        username="northstar_admin",

    )

    assert log_id == 1



    rows = fetch_sentry_logs(conn, resolution_status="unresolved", limit=5)

    assert len(rows) == 1

    assert rows[0].exception_type == "RuntimeError"

    assert rows[0].error_message == "scheduler engine failure"

    assert rows[0].tenant_id == "tenant-a"

    assert rows[0].username == "northstar_admin"

    assert rows[0].resolution_status == "unresolved"

    assert rows[0].proposed_patch_code is None





def test_extract_exception_origin_prefers_project_frame() -> None:

    def _inner() -> None:

        raise ValueError("bad shift assignment")



    try:

        _inner()

    except ValueError as exc:

        exc_type, message, target_file, line_number, tb = extract_exception_origin(exc)

        assert exc_type == "ValueError"

        assert message == "bad shift assignment"

        assert "test_sentry_watcher.py" in (target_file or "")

        assert line_number is not None

        assert "bad shift assignment" in tb





def test_generate_llm_diagnostic_packet_markdown() -> None:

    conn = _memory_db()

    log_unhandled_exception(

        conn,

        KeyError("missing template"),

        tenant_id="tenant-b",

        username="southbridge_admin",

    )

    packet = generate_llm_diagnostic_packet(conn, limit=5)

    assert "# Lab Staffing Scheduler — Sentry Diagnostic Packet" in packet

    assert "log_id `1`" in packet

    assert "missing template" in packet

    assert "Instructions for Patch Agent" in packet





def test_generate_llm_diagnostic_packet_empty() -> None:

    conn = _memory_db()

    packet = generate_llm_diagnostic_packet(conn)

    assert "No unresolved sentry logs were found." in packet





def test_update_sentry_log_for_review_persists_patch_code() -> None:

    conn = _memory_db()

    log_id = log_unhandled_exception(conn, RuntimeError("needs review"))

    update_sentry_log_for_review(conn, log_id, "--- a/file\n+++ b/file\n")

    rows = fetch_sentry_logs(conn, resolution_status="awaiting_review", limit=5)

    assert len(rows) == 1

    assert rows[0].log_id == log_id

    assert rows[0].proposed_patch_code == "--- a/file\n+++ b/file\n"

