from lab_scheduler.errors.schedule_error import (
    ScheduleError,
    schedule_error_from_code,
    require_schedule_error,
)


def test_schedule_error_registry_covers_all_members() -> None:
    assert len(ScheduleError) == len(ScheduleError.__members__)
    for member in ScheduleError:
        assert member.meta.category
        assert member.meta.manager_label
        assert member.value == member


def test_legacy_impossible_coverage_alias_resolves() -> None:
    assert schedule_error_from_code("IMPOSSIBLE_COVERAGE") is ScheduleError.ERR_IMPOSSIBLE_COVERAGE
    assert schedule_error_from_code("ERR_IMPOSSIBLE_COVERAGE") is ScheduleError.ERR_IMPOSSIBLE_COVERAGE


def test_clinical_floor_factory() -> None:
    assert ScheduleError.clinical_floor("EVENING") is ScheduleError.CLINICAL_EVENING
    assert ScheduleError.clinical_floor("night") is ScheduleError.CLINICAL_NIGHT


def test_stretch_codes_match_legacy_constants() -> None:
    from lab_scheduler.compliance.compliance_rules import (
        APPROVED_STRETCH_CODE,
        CONSECUTIVE_DAYS_WARNING_CODE,
        JOANNE_STYLE_STRETCH_CODE,
    )

    assert APPROVED_STRETCH_CODE == ScheduleError.APPROVED_STRETCH.value
    assert JOANNE_STYLE_STRETCH_CODE == ScheduleError.JOANNE_STYLE_CLINICAL_STRETCH.value
    assert CONSECUTIVE_DAYS_WARNING_CODE == ScheduleError.CONSECUTIVE_DAYS_WARNING.value


def test_violation_kind_legacy_constants() -> None:
    from lab_scheduler.engine.constraints import (
        VIOLATION_COVERAGE_TARGET,
        VIOLATION_IMPOSSIBLE_COVERAGE,
        VIOLATION_LABOR_RULE,
    )

    assert VIOLATION_LABOR_RULE == ScheduleError.LABOR_RULE.value
    assert VIOLATION_COVERAGE_TARGET == ScheduleError.COVERAGE_TARGET.value
    assert VIOLATION_IMPOSSIBLE_COVERAGE == ScheduleError.ERR_IMPOSSIBLE_COVERAGE.value


def test_require_schedule_error_raises_for_unknown_code() -> None:
    try:
        require_schedule_error("NOT_A_REAL_CODE")
    except KeyError as exc:
        assert "NOT_A_REAL_CODE" in str(exc)
    else:
        raise AssertionError("expected KeyError")


def test_overtime_bypass_manager_label() -> None:
    from lab_scheduler.errors.schedule_error import OVERTIME_COMPLIANCE_BYPASS_LABEL

    assert (
        OVERTIME_COMPLIANCE_BYPASS_LABEL
        == ScheduleError.OVERTIME_REQUIRED_COMPLIANCE_BYPASSED.meta.manager_label
    )
