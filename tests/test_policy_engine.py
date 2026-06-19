from datetime import date

import pandas as pd

from lab_scheduler.compliance import MANITOBA, ShiftTemplateInfo
from lab_scheduler.policy.policy_engine import (
    CellMutation,
    SchedulePolicyEngine,
    compute_biweekly_ot_risk,
    flush_pending_mutations,
)
from lab_scheduler.scheduling.auto_generate import EmployeeProfile


def _templates() -> dict[str, dict[str, object]]:
    return {
        "shift-morning": {
            "id": "shift-morning",
            "code": "MORNING",
            "short": "D",
            "duration_minutes": 480,
        },
        "shift-evening": {
            "id": "shift-evening",
            "code": "EVENING",
            "short": "E",
            "duration_minutes": 480,
        },
        "shift-night": {
            "id": "shift-night",
            "code": "NIGHT",
            "short": "N",
            "duration_minutes": 480,
        },
    }


def _template_info() -> dict[str, ShiftTemplateInfo]:
    return {
        "shift-morning": ShiftTemplateInfo(
            "shift-morning", "MORNING", "Morning", "07:00", "15:00", 480, False
        ),
        "shift-evening": ShiftTemplateInfo(
            "shift-evening", "EVENING", "Evening", "15:00", "23:00", 480, False
        ),
        "shift-night": ShiftTemplateInfo(
            "shift-night", "NIGHT", "Night", "23:00", "07:00", 480, True
        ),
    }


def _shift_quals() -> dict[str, set[str]]:
    return {
        "shift-morning": {"qual-mlt"},
        "shift-evening": {"qual-mlt"},
        "shift-night": {"qual-mlt"},
    }


def _employee_row(employee_id: str, name: str, *, contract: str = "D/E") -> dict[str, object]:
    return {
        "Employee": name,
        "employee_id": employee_id,
        "fte": 1.0,
        "contract_line_type": contract,
    }


def _build_frame(dates: list[date], rows: list[dict[str, object]]) -> pd.DataFrame:
    frame_rows = []
    for row in rows:
        built = dict(row)
        for day in dates:
            built.setdefault(day.isoformat(), "—")
        frame_rows.append(built)
    return pd.DataFrame(frame_rows)


def test_derive_view_model_flags_union_risk_and_off_target_tally() -> None:
    dates = [date(2026, 6, 1), date(2026, 6, 2)]
    employees = [
        _employee_row("emp-a", "Vacant MLT D/E - Line 01"),
        _employee_row("emp-b", "Vacant MLT D/E - Line 02"),
    ]
    draft = _build_frame(
        dates,
        [
            {**employees[0], date(2026, 6, 1).isoformat(): "D"},
            {**employees[1], date(2026, 6, 1).isoformat(): "D"},
        ],
    )
    engine = SchedulePolicyEngine()
    view = engine.derive_view_model(
        draft,
        employees=employees,
        dates=dates,
        week_count=8,
    )
    assert view.off_target_cells
    assert view.off_target_cells[0].band == "D"
    assert view.contract_rows["emp-a"].status_class == "contract-union-risk"


def test_apply_mutations_stages_valid_edit_and_updates_tallies() -> None:
    dates = [date(2026, 6, 1)]
    employees = [
        {
            "id": "emp-a",
            "full_name": "Vacant MLT D/E - Line 01",
            "fte": 1.0,
            "contract_line_type": "D/E",
        }
    ]
    employee_rows = [_employee_row("emp-a", employees[0]["full_name"])]
    draft = _build_frame(dates, employee_rows)
    edited = draft.copy()
    edited.at[0, dates[0].isoformat()] = "D"

    profiles = {
        "emp-a": EmployeeProfile(
            "emp-a",
            employees[0]["full_name"],
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        )
    }
    engine = SchedulePolicyEngine()
    view, applied, _ = engine.apply_mutations(
        draft_frame=draft,
        edited_frame=edited,
        employees=employees,
        dates=dates,
        templates=_templates(),
        template_info=_template_info(),
        shift_quals=_shift_quals(),
        rules=MANITOBA,
        period_start=dates[0],
        period_end=dates[0],
        weeks_in_period=8,
        profiles_by_id=profiles,
    )
    assert applied is True
    assert view.has_unpublished_changes is True
    assert len(view.pending_mutations) == 1
    assert view.tallies.days[dates[0].isoformat()] == 1


def test_apply_mutations_matches_employees_to_sorted_draft_rows() -> None:
    day_key = date(2026, 6, 6).isoformat()
    dates = [date(2026, 6, 6)]
    employees = [
        {
            "id": "emp-a",
            "full_name": "Vacant MLT D/E - Line 01",
            "fte": 1.0,
            "contract_line_type": "D/E",
        },
        {
            "id": "emp-b",
            "full_name": "Vacant MLT D/E - Line 02",
            "fte": 1.0,
            "contract_line_type": "D/E",
        },
    ]
    draft = pd.DataFrame(
        [
            {
                "employee_id": "emp-b",
                "Employee": employees[1]["full_name"],
                day_key: "",
            },
            {
                "employee_id": "emp-a",
                "Employee": employees[0]["full_name"],
                day_key: "D",
            },
        ]
    )
    edited = draft.copy()
    edited.at[1, day_key] = ""

    profiles = {
        employee_id: EmployeeProfile(
            employee_id,
            employee["full_name"],
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        )
        for employee_id, employee in (
            ("emp-a", employees[0]),
            ("emp-b", employees[1]),
        )
    }
    engine = SchedulePolicyEngine()
    view, applied, _ = engine.apply_mutations(
        draft_frame=draft,
        edited_frame=edited,
        employees=employees,
        dates=dates,
        templates=_templates(),
        template_info=_template_info(),
        shift_quals=_shift_quals(),
        rules=MANITOBA,
        period_start=dates[0],
        period_end=dates[0],
        weeks_in_period=8,
        profiles_by_id=profiles,
    )
    assert applied is True
    assert len(view.pending_mutations) == 1
    mutation = view.pending_mutations[0]
    assert mutation.employee_id == "emp-a"
    assert mutation.previous_token == "D"
    assert mutation.new_token == ""


def test_apply_mutations_rejects_invalid_contract_line() -> None:
    dates = [date(2026, 6, 1)]
    employees = [
        {
            "id": "emp-a",
            "full_name": "Vacant MLT D/E - Line 01",
            "fte": 1.0,
            "contract_line_type": "D/E",
        }
    ]
    draft = _build_frame(dates, [_employee_row("emp-a", employees[0]["full_name"])])
    edited = draft.copy()
    edited.at[0, dates[0].isoformat()] = "N"

    profiles = {
        "emp-a": EmployeeProfile(
            "emp-a",
            employees[0]["full_name"],
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        )
    }
    engine = SchedulePolicyEngine()
    view, applied, toasts = engine.apply_mutations(
        draft_frame=draft,
        edited_frame=edited,
        employees=employees,
        dates=dates,
        templates=_templates(),
        template_info=_template_info(),
        shift_quals=_shift_quals(),
        rules=MANITOBA,
        period_start=dates[0],
        period_end=dates[0],
        weeks_in_period=8,
        profiles_by_id=profiles,
        enforce_assignment_rules=True,
    )
    assert applied is False
    assert view.pending_mutations == []
    assert view.cell_errors


def test_compute_biweekly_ot_risk() -> None:
    from datetime import timedelta

    dates = [date(2026, 6, 1) + timedelta(days=offset) for offset in range(14)]
    row = {day.isoformat(): "D" for day in dates}
    assert compute_biweekly_ot_risk(row, dates) is True

    light_row = {dates[0].isoformat(): "D", dates[1].isoformat(): "E"}
    assert compute_biweekly_ot_risk(light_row, dates[:2]) is False


def test_flush_pending_mutations() -> None:
    calls: list[tuple[str, date]] = []

    def persist_cell_change(**kwargs: object) -> tuple[bool, str]:
        calls.append((str(kwargs["employee_id"]), kwargs["assignment_date"]))  # type: ignore[arg-type]
        return True, ""

    pending = [
        CellMutation("emp-a", date(2026, 6, 1), "", "D"),
        CellMutation("emp-b", date(2026, 6, 2), "E", ""),
    ]
    applied, errors = flush_pending_mutations(pending, persist_cell_change=persist_cell_change)
    assert applied == 2
    assert errors == []
    assert len(calls) == 2
