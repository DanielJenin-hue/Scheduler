from datetime import date, timedelta

import pytest

from lab_scheduler.scheduling.breakroom_print import (
    TRIAGE_ESCALATED_CELL_TAG as BREAKROOM_TRIAGE_TAG,
    build_coverage_gaps_by_day,
    format_breakroom_shift_cell,
    format_schedule_employee_label,
    generate_breakroom_print_html,
    normalize_breakroom_cell,
)


def _single_line_standard_schedule():
    """A STANDARD line that works Mon-Fri (D) and is OFF Sat/Sun (blank)."""
    start = date(2026, 6, 1)
    dates = [start + timedelta(days=i) for i in range(7)]
    row = {"Employee": "A MLT", "employee_id": "e1"}
    for i, d in enumerate(dates):
        row[d.isoformat()] = "D" if i < 5 else ""
    employees = [{"id": "e1", "full_name": "A MLT", "fte": 1.0, "contract_line_type": "D/E"}]
    base = dict(
        facility_name="F",
        period_name="P",
        period_start=dates[0],
        period_end=dates[-1],
        week_count=1,
        employees=employees,
        dates=dates,
        schedule_rows=[row],
        schedule_archetype="STANDARD",
    )
    return base, dates


def test_blank_days_off_are_not_flagged_open_by_default() -> None:
    # Regression: legitimate days off must NOT render as "+OPEN" pickups. With no
    # explicit gap signal, every blank employee cell stays visually quiet.
    base, _ = _single_line_standard_schedule()
    html = generate_breakroom_print_html(**base)
    assert "open-shift-text</span>" not in html  # no per-cell OPEN markers
    assert "Coverage Gaps (open seats)" not in html


def test_true_gaps_render_in_dedicated_coverage_row() -> None:
    base, dates = _single_line_standard_schedule()
    gaps = build_coverage_gaps_by_day(
        [
            type("S", (), {"assignment_date": dates[5]})(),
            type("S", (), {"assignment_date": dates[5]})(),
            type("S", (), {"assignment_date": dates[6]})(),
        ]
    )
    html = generate_breakroom_print_html(**base, coverage_gaps_by_day=gaps)
    assert "Coverage Gaps (open seats)" in html
    assert "coverage-gap-cell" in html
    assert "3 open total" in html


def test_explicit_open_shift_cells_are_marked() -> None:
    base, dates = _single_line_standard_schedule()
    html = generate_breakroom_print_html(**base, open_shift_cells={("e1", dates[5])})
    # Exactly one rendered OPEN cell (the CSS rule also contains the class name).
    assert html.count("<span class='open-shift'>") == 1
from lab_scheduler.scheduling.schedule_export import (
    TRIAGE_ESCALATED_CELL_TAG,
    apply_triage_escalation_tags,
    filter_breakroom_export_rows,
    render_breakroom_schedule_html,
    _sanitize_stale_triage_collisions,
)


def test_apply_triage_escalation_tags_matches_slot_and_date() -> None:
    dates = [date(2026, 6, 12), date(2026, 6, 13)]
    rows = [
        {
            "Employee": "Vacant MLT D/N - Line 03",
            "employee_id": "portage-mlt-03",
            "fte": 1.0,
            "contract_line_type": "D/N",
            "2026-06-12": "—",
            "2026-06-13": "D",
        }
    ]
    triage = [
        {
            "slot": "Vacant MLT D/N - Line 03",
            "date": "2026-06-12",
            "error_code": "ERR_IMPOSSIBLE_COVERAGE",
            "blocked_by": "MAX_WEEKLY_HOURS",
        }
    ]

    tagged = apply_triage_escalation_tags(rows, triage, dates)

    assert tagged[0]["2026-06-12"] == TRIAGE_ESCALATED_CELL_TAG
    assert tagged[0]["2026-06-13"] == "D"


def test_apply_triage_escalation_tags_supports_date_object_column_keys() -> None:
    day = date(2026, 6, 12)
    rows = [
        {
            "Employee": "Vacant MLT D/N - Line 03",
            "employee_id": "portage-mlt-03",
            day: "—",
        }
    ]
    triage = [
        {
            "slot": "Vacant MLT D/N - Line 03",
            "date": "2026-06-12",
        }
    ]

    tagged = apply_triage_escalation_tags(rows, triage, [day])

    assert tagged[0][day] == TRIAGE_ESCALATED_CELL_TAG


def test_apply_triage_escalation_tags_skips_assigned_cells() -> None:
    """Stale triage must not co-mark a cell that already has a shift assignment."""
    dates = [date(2026, 6, 12)]
    rows = [
        {
            "Employee": "Vacant MLT D/E - Line 01",
            "employee_id": "portage-mlt-01",
            "2026-06-12": "E",
        }
    ]
    triage = [
        {
            "slot": "Vacant MLT D/E - Line 01",
            "date": "2026-06-12",
            "shift_code": "MORNING",
        }
    ]

    tagged = apply_triage_escalation_tags(rows, triage, dates)

    assert tagged[0]["2026-06-12"] == "E"
    assert TRIAGE_ESCALATED_CELL_TAG not in str(tagged[0]["2026-06-12"])


def test_sanitize_stale_triage_collisions_strips_compound_cells() -> None:
    dates = [date(2026, 6, 12)]
    compound = f"E | {TRIAGE_ESCALATED_CELL_TAG}"
    rows = [
        {
            "Employee": "Vacant MLT D/E - Line 01",
            "employee_id": "portage-mlt-01",
            "2026-06-12": compound,
        }
    ]
    cleaned = _sanitize_stale_triage_collisions(rows, dates)
    assert cleaned[0]["2026-06-12"] == "E"


def test_normalize_breakroom_cell_preserves_compound_triage_value() -> None:
    compound = f"E | {TRIAGE_ESCALATED_CELL_TAG}"
    assert normalize_breakroom_cell(compound) == compound


def test_format_schedule_employee_label_inlines_role_and_target_hours() -> None:
    assert (
        format_schedule_employee_label("Vacant MLT D/E - Line 01", target_hours=320.0)
        == "Vacant MLT D/E - Line 01 (320h)"
    )
    assert (
        format_schedule_employee_label(
            "Avery Miller",
            role_code="MLT",
            target_hours=160.0,
        )
        == "Avery Miller · MLT (160h)"
    )


def test_format_breakroom_shift_cell_renders_colored_tokens() -> None:
    day_html = format_breakroom_shift_cell("D")
    evening_html = format_breakroom_shift_cell("E")
    night_html = format_breakroom_shift_cell("N")
    assert "print-token-d" in day_html
    assert "#dbeafe" in day_html
    assert "print-token-e" in evening_html
    assert "#fef3c7" in evening_html
    assert "print-token-n" in night_html
    assert "#1e293b" in night_html


def test_format_breakroom_shift_cell_assignment_wins_over_triage_tag() -> None:
    # An assigned shift and an unfilled-escalated gap are mutually exclusive
    # states; when a shift token is present it must win and the triage tag must
    # NOT be co-rendered in the same cell.
    compound = f"E | {BREAKROOM_TRIAGE_TAG}"
    html = format_breakroom_shift_cell(normalize_breakroom_cell(compound))
    assert "print-token-e" in html
    assert "triage-escalated-tag" not in html
    assert BREAKROOM_TRIAGE_TAG not in html


def test_format_breakroom_shift_cell_triage_tag_only_when_unassigned() -> None:
    # An unassigned-but-flagged cell carries the bare triage tag (no shift part);
    # the triage tag is the only thing that renders, with no shift print-token.
    html = format_breakroom_shift_cell(normalize_breakroom_cell(BREAKROOM_TRIAGE_TAG))
    assert "triage-escalated-tag" in html
    assert BREAKROOM_TRIAGE_TAG in html
    assert "print-token" not in html


def test_apply_triage_escalation_tags_adds_row_for_unknown_slot() -> None:
    dates = [date(2026, 6, 18)]
    tagged = apply_triage_escalation_tags(
        [],
        [
            {
                "slot": "Vacant MLA D/N - Line 04",
                "date": "2026-06-18",
                "error_code": "ERR_IMPOSSIBLE_COVERAGE",
                "blocked_by": "CONTRACT_FTE_160",
            }
        ],
        dates,
    )

    assert len(tagged) == 1
    assert tagged[0]["Employee"] == "Vacant MLA D/N - Line 04"
    assert tagged[0]["2026-06-18"] == TRIAGE_ESCALATED_CELL_TAG


def test_apply_triage_escalation_tags_skips_optional_supplemental_entries() -> None:
    dates = [date(2026, 6, 30)]
    rows = [
        {
            "Employee": "Vacant MLT D/E - Line 01",
            "employee_id": "portage-mlt-01",
            "2026-06-30": "—",
        }
    ]
    triage = [
        {
            "slot": "Vacant MLA D/E - Line 952",
            "slot_id": (
                "2026-06-30|MORNING|shift-morning|Smooth Day Balance - MLA - Day 22|"
                "seat=951|qual=MLA"
            ),
            "date": "2026-06-30",
        },
        {
            "slot": "Vacant MLT D/E - Line 01",
            "date": "2026-06-30",
        },
    ]

    tagged = apply_triage_escalation_tags(rows, triage, dates)

    assert len(tagged) == 1
    assert tagged[0]["2026-06-30"] == TRIAGE_ESCALATED_CELL_TAG


def test_filter_breakroom_export_rows_drops_supplemental_ghost_lines() -> None:
    rows = [
        {
            "Employee": "Vacant MLT D/E - Line 01",
            "employee_id": "portage-mlt-01",
        },
        {
            "Employee": "Vacant MLA D/E - Line 952",
            "employee_id": "",
        },
    ]

    filtered = filter_breakroom_export_rows(rows)

    assert len(filtered) == 1
    assert filtered[0]["Employee"] == "Vacant MLT D/E - Line 01"


def test_render_breakroom_schedule_html_injects_escalated_tag(tmp_path) -> None:
    triage_path = tmp_path / "Triage_Escalation_2026-05-27.json"
    triage_path.write_text(
        """
        {
          "triage_list": [
            {
              "slot": "Vacant MLT D/N - Line 03",
              "date": "2026-06-12",
              "error_code": "ERR_IMPOSSIBLE_COVERAGE",
              "blocked_by": "MAX_WEEKLY_HOURS"
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    employees = [
        {
            "id": "portage-mlt-03",
            "full_name": "Vacant MLT D/N - Line 03",
            "fte": 1.0,
            "contract_line_type": "D/N",
        }
    ]
    dates = [date(2026, 6, 12)]
    schedule_rows = [
        {
            "Employee": "Vacant MLT D/N - Line 03",
            "employee_id": "portage-mlt-03",
            "fte": 1.0,
            "contract_line_type": "D/N",
            "2026-06-12": "—",
        }
    ]

    tagged_rows, html = render_breakroom_schedule_html(
        schedule_rows=schedule_rows,
        employees=employees,
        dates=dates,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        week_count=4,
        triage_escalation_path=triage_path,
    )

    assert tagged_rows[0]["2026-06-12"] == TRIAGE_ESCALATED_CELL_TAG
    assert "triage-escalated-tag" in html
    assert "[UNFILLED - ESCALATED]" in html


def test_render_breakroom_schedule_html_surfaces_night_streak_violations() -> None:
    dates = [date(2026, 6, 10) + timedelta(days=offset) for offset in range(6)]
    row = {
        "Employee": "Vacant MLT D/N - Line 01",
        "employee_id": "portage-mlt-01",
        "fte": 1.0,
        "contract_line_type": "D/N",
    }
    for index, day in enumerate(dates):
        row[day.isoformat()] = "N" if index < 5 else "D"

    _, html = render_breakroom_schedule_html(
        schedule_rows=[row],
        employees=[
            {
                "id": "portage-mlt-01",
                "full_name": "Vacant MLT D/N - Line 01",
                "fte": 1.0,
                "contract_line_type": "D/N",
            }
        ],
        dates=dates,
        period_start=dates[0],
        period_end=dates[-1],
        week_count=1,
    )

    assert "Night Shift Sequence Violations" in html
    assert "NIGHT_STREAK" in html


def test_render_breakroom_schedule_html_surfaces_work_streak_violations() -> None:
    start = date(2026, 6, 9)
    dates = [start + timedelta(days=offset) for offset in range(15)]
    row = {
        "Employee": "Vacant MLA D/E - Line 08",
        "employee_id": "portage-mla-08",
        "fte": 1.0,
        "contract_line_type": "D/E",
    }
    for index, day in enumerate(dates):
        row[day.isoformat()] = "E" if index % 3 == 1 else "D" if index < 11 else "—"

    _, html = render_breakroom_schedule_html(
        schedule_rows=[row],
        employees=[
            {
                "id": "portage-mla-08",
                "full_name": "Vacant MLA D/E - Line 08",
                "fte": 1.0,
                "contract_line_type": "D/E",
            }
        ],
        dates=dates,
        period_start=dates[0],
        period_end=dates[-1],
        week_count=8,
    )

    assert "Consecutive Work-Day Violations" in html
    assert "WORK_STREAK" in html
    assert "portage-mla-08" not in html or "11 consecutive work days" in html


def test_generate_breakroom_print_html_renders_pre_tagged_cell() -> None:
    html = generate_breakroom_print_html(
        facility_name="Northstar Lab",
        period_name="Summer 2026",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        week_count=4,
        employees=[
            {
                "id": "portage-mlt-03",
                "full_name": "Vacant MLT D/N - Line 03",
                "fte": 1.0,
                "contract_line_type": "D/N",
            }
        ],
        dates=[date(2026, 6, 12)],
        schedule_rows=[
            {
                "Employee": "Vacant MLT D/N - Line 03",
                "employee_id": "portage-mlt-03",
                "fte": 1.0,
                "contract_line_type": "D/N",
                "2026-06-12": TRIAGE_ESCALATED_CELL_TAG,
            }
        ],
    )

    assert "[UNFILLED - ESCALATED]" in html
    assert "triage-escalated-tag" in html
