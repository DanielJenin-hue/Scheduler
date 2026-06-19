from __future__ import annotations

import logging
from datetime import date

import pytest

from lab_scheduler.paths import resolve_project_path
from lab_scheduler.workers.export_worker import ExportWorkerInput, run_export_worker


def test_resolve_project_path_anchors_relative_exports_to_root(tmp_path) -> None:
    exports = tmp_path / "exports"
    exports.mkdir()
    triage_file = exports / "Triage_Escalation_2026-05-27.json"
    triage_file.write_text('{"triage_list": []}', encoding="utf-8")

    scripts_cwd = tmp_path / "scripts"
    scripts_cwd.mkdir()

    resolved = resolve_project_path(tmp_path, "exports/Triage_Escalation_2026-05-27.json")
    assert resolved == triage_file.resolve()
    assert resolved.is_file()


def test_export_worker_logs_error_when_triage_path_missing(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="lab_scheduler.workers.export_worker")

    run_export_worker(
        tmp_path,
        ExportWorkerInput(
            assignments=[],
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 7),
            triage_escalation_path="exports/Triage_Escalation_2026-05-27.json",
        ),
        report_date=date(2026, 5, 27),
    )

    assert any(
        "Triage escalation file missing on disk" in record.message for record in caplog.records
    )
