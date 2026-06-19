
import pytest

pytestmark = pytest.mark.legacy

from datetime import date

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.engine.demand import expand_schedule_slots, portage_concurrent_demands
from lab_scheduler.scheduling.auto_generate import (
    EmployeeProfile,
    _EmployeeState,
    _run_clinical_seat_lockdown_pass,
)
from lab_scheduler.scheduling.clinical_seats import (
    clinical_seat_number,
    mandatory_clinical_candidates_with_audit,
    select_mandatory_clinical_candidate,
)
from lab_scheduler.scheduling.provisional_constants import CONTRACT_LINE_EXCEPTION_VIOLATION_CODE
from lab_scheduler.simulation.hospital_stress import (
    QUAL_MLA,
    QUAL_MLT,
    shift_required_qualifications,
    shift_templates,
)


def test_contract_line_conflict_is_provisional_for_clinical_seat_only() -> None:
    period_day = date(2026, 6, 1)
    templates = shift_templates()
    expanded = expand_schedule_slots(
        period_start=period_day,
        period_end=period_day,
        shift_templates=templates,
        concurrent_demands=portage_concurrent_demands(),
    )
    night_seat = next(
        slot
        for slot in expanded
        if templates[slot.shift_id].code == "NIGHT"
        and slot.assignment_date == period_day
        and clinical_seat_number(slot.role_pool_id) == 2
    )
    night_template = templates[night_seat.shift_id]
    de_mla = EmployeeProfile(
        "mla-de",
        "MLA D/E Borrow",
        1.0,
        {QUAL_MLA},
        contract_line_type="D/E",
    )
    qual_codes = {QUAL_MLT: "MLT", QUAL_MLA: "MLA"}
    states = {
        "mla-de": _EmployeeState(profile=de_mla, target_hours=160.0, total_hours=0.0),
    }
    required = shift_required_qualifications()[night_seat.shift_id]

    audit = mandatory_clinical_candidates_with_audit(
        employees=[de_mla],
        required=required,
        states=states,
        assignment_date=period_day,
        template=night_template,
        qual_codes=qual_codes,
        required_qual_code=night_seat.required_qual_code,
        availability_blocked=None,
        role_pool_id=night_seat.role_pool_id,
    )
    chosen, provisional = select_mandatory_clinical_candidate(audit)
    assert chosen is de_mla
    assert provisional is not None
    assert "Contract Line" in provisional.violation_message
    assert de_mla in audit.eligible
    assert audit.provisional_contract_line


def test_contract_line_stays_hard_block_outside_clinical_seats() -> None:
    night_template = shift_templates()["shift-night"]
    de_mlt = EmployeeProfile(
        "mlt-de",
        "MLT D/E",
        1.0,
        {QUAL_MLT},
        contract_line_type="D/E",
    )
    qual_codes = {QUAL_MLT: "MLT"}
    states = {
        "mlt-de": _EmployeeState(profile=de_mlt, target_hours=160.0, total_hours=0.0),
    }

    audit = mandatory_clinical_candidates_with_audit(
        employees=[de_mlt],
        required={QUAL_MLT},
        states=states,
        assignment_date=date(2026, 6, 3),
        template=night_template,
        qual_codes=qual_codes,
        required_qual_code="MLT",
        availability_blocked=None,
        role_pool_id="bench-mlt-dn",
    )
    chosen, provisional = select_mandatory_clinical_candidate(audit)
    assert chosen is None
    assert provisional is None
    assert audit.rejections


def test_lockdown_pass_tags_contract_line_exception_assignment() -> None:
    period_day = date(2026, 6, 1)
    templates = shift_templates()
    expanded = expand_schedule_slots(
        period_start=period_day,
        period_end=period_day,
        shift_templates=templates,
        concurrent_demands=portage_concurrent_demands(),
    )
    employees = [
        EmployeeProfile("mlt-eve", "MLT Eve", 1.0, {QUAL_MLT}, contract_line_type="D/E"),
        EmployeeProfile("mla-eve", "MLA Eve", 1.0, {QUAL_MLA}, contract_line_type="D/E"),
        EmployeeProfile("mlt-night", "MLT Night", 1.0, {QUAL_MLT}, contract_line_type="D/E"),
        EmployeeProfile("mla-night", "MLA Night", 1.0, {QUAL_MLA}, contract_line_type="D/E"),
    ]
    qual_codes = {QUAL_MLT: "MLT", QUAL_MLA: "MLA"}
    states = {
        employee.id: _EmployeeState(profile=employee, target_hours=160.0, total_hours=0.0)
        for employee in employees
    }
    fill_counts: dict = {}
    planned = _run_clinical_seat_lockdown_pass(
        employees=employees,
        states=states,
        expanded_slots=expanded,
        shift_templates=templates,
        shift_required_qualifications=shift_required_qualifications(),
        rules=MANITOBA,
        fill_counts=fill_counts,
        availability_blocked=None,
        qual_codes=qual_codes,
        log_critical_gaps=False,
    )
    night_assignments = [
        assignment
        for assignment in planned
        if templates[assignment.shift_template_id].code == "NIGHT"
    ]
    assert night_assignments
    assert any(assignment.contract_line_exception for assignment in night_assignments)


def test_merged_eligible_prefers_strict_contract_line_match() -> None:
    period_day = date(2026, 6, 1)
    templates = shift_templates()
    expanded = expand_schedule_slots(
        period_start=period_day,
        period_end=period_day,
        shift_templates=templates,
        concurrent_demands=portage_concurrent_demands(),
    )
    night_seat = next(
        slot
        for slot in expanded
        if templates[slot.shift_id].code == "NIGHT"
        and slot.assignment_date == period_day
        and clinical_seat_number(slot.role_pool_id) == 2
    )
    de_mla = EmployeeProfile(
        "mla-de",
        "MLA D/E Borrow",
        1.0,
        {QUAL_MLA},
        contract_line_type="D/E",
    )
    dn_mla = EmployeeProfile(
        "mla-dn",
        "MLA D/N Strict",
        1.0,
        {QUAL_MLA},
        contract_line_type="D/N",
    )
    qual_codes = {QUAL_MLT: "MLT", QUAL_MLA: "MLA"}
    states = {
        "mla-de": _EmployeeState(profile=de_mla, target_hours=160.0, total_hours=0.0),
        "mla-dn": _EmployeeState(profile=dn_mla, target_hours=160.0, total_hours=0.0),
    }
    required = shift_required_qualifications()[night_seat.shift_id]
    audit = mandatory_clinical_candidates_with_audit(
        employees=[de_mla, dn_mla],
        required=required,
        states=states,
        assignment_date=period_day,
        template=templates[night_seat.shift_id],
        qual_codes=qual_codes,
        required_qual_code=night_seat.required_qual_code,
        availability_blocked=None,
        role_pool_id=night_seat.role_pool_id,
    )
    chosen, provisional = select_mandatory_clinical_candidate(audit)
    assert chosen is dn_mla
    assert provisional is None
    assert de_mla in audit.eligible
    assert any(item.employee.id == "mla-de" for item in audit.provisional_contract_line)


def test_mlt_mla_qualification_boundaries_remain_strict() -> None:
    period_day = date(2026, 6, 1)
    templates = shift_templates()
    expanded = expand_schedule_slots(
        period_start=period_day,
        period_end=period_day,
        shift_templates=templates,
        concurrent_demands=portage_concurrent_demands(),
    )
    mlt_seat = next(
        slot
        for slot in expanded
        if templates[slot.shift_id].code == "EVENING"
        and slot.assignment_date == period_day
        and slot.required_qual_code == "MLT"
    )
    mla_only = EmployeeProfile(
        "mla-dn",
        "MLA D/N",
        1.0,
        {QUAL_MLA},
        contract_line_type="D/N",
    )
    qual_codes = {QUAL_MLT: "MLT", QUAL_MLA: "MLA"}
    states = {
        "mla-dn": _EmployeeState(profile=mla_only, target_hours=160.0, total_hours=0.0),
    }
    audit = mandatory_clinical_candidates_with_audit(
        employees=[mla_only],
        required=shift_required_qualifications()[mlt_seat.shift_id],
        states=states,
        assignment_date=period_day,
        template=templates[mlt_seat.shift_id],
        qual_codes=qual_codes,
        required_qual_code="MLT",
        availability_blocked=None,
        role_pool_id=mlt_seat.role_pool_id,
    )
    chosen, provisional = select_mandatory_clinical_candidate(audit)
    assert chosen is None
    assert provisional is None
    assert any("seat qual mismatch" in reason for reason in audit.rejections)
