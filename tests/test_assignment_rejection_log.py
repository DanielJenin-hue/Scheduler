from datetime import date
from io import StringIO
import sys

from lab_scheduler.scheduling.assignment_rejection_log import log_assignment_rejection


def test_log_assignment_rejection_prints_expected_format(capsys) -> None:
    log_assignment_rejection(
        "portage-mla-01",
        date(2026, 6, 15),
        "would violate 15h turnaround (8.0h gap; requires 15h)",
    )
    captured = capsys.readouterr()
    assert (
        captured.out.strip()
        == "REJECTED: portage-mla-01 on 2026-06-15 due to would violate 15h turnaround (8.0h gap; requires 15h)"
    )


def test_log_assignment_rejection_writes_to_stdout_directly() -> None:
    buffer = StringIO()
    original_stdout = sys.stdout
    sys.stdout = buffer
    try:
        log_assignment_rejection(
            "emp-a1",
            date(2026, 6, 2),
            "would exceed 6 consecutive work days (fatigue guardrail)",
        )
    finally:
        sys.stdout = original_stdout
    assert "REJECTED: emp-a1 on 2026-06-02 due to" in buffer.getvalue()


def test_log_assignment_rejection_survives_stdout_oserror(monkeypatch) -> None:
    class _BrokenStdout:
        def write(self, _text: str) -> int:
            raise OSError(22, "Invalid argument")

        def flush(self) -> None:
            raise OSError(22, "Invalid argument")

    monkeypatch.setattr(sys, "stdout", _BrokenStdout())
    log_assignment_rejection("emp-a1", date(2026, 6, 2), "seat qual mismatch")


def test_emit_scheduling_trace_survives_stdout_oserror(monkeypatch) -> None:
    from lab_scheduler.scheduling.assignment_rejection_log import emit_scheduling_trace

    class _BrokenStdout:
        def write(self, _text: str) -> int:
            raise OSError(22, "Invalid argument")

        def flush(self) -> None:
            raise OSError(22, "Invalid argument")

    monkeypatch.setattr(sys, "stdout", _BrokenStdout())
    emit_scheduling_trace("CLINICAL_LOCKDOWN_DEBUG seat=Seat_02")
