from lab_scheduler.scheduling.auto_generate import EmployeeProfile
from lab_scheduler.scheduling.seniority_ranking import (
    cba_rank_key,
    evaluate_seniority_bypass,
    rank_profiles_cba,
)


def test_cba_rank_key_prefers_seniority_then_part_time_then_wage() -> None:
    senior_full = EmployeeProfile(
        "emp-senior",
        "Senior Tech",
        1.0,
        {"qual-mlt"},
        seniority_hours=9000.0,
        base_hourly_rate=42.0,
    )
    junior_part = EmployeeProfile(
        "emp-junior",
        "Junior Tech",
        0.6,
        {"qual-mlt"},
        seniority_hours=2000.0,
        base_hourly_rate=26.0,
    )
    senior_part_cheaper = EmployeeProfile(
        "emp-tie",
        "Tie Tech",
        0.8,
        {"qual-mlt"},
        seniority_hours=9000.0,
        base_hourly_rate=38.0,
    )

    ranked = rank_profiles_cba([junior_part, senior_full, senior_part_cheaper])
    assert [profile.id for profile in ranked] == [
        "emp-tie",
        "emp-senior",
        "emp-junior",
    ]
    assert cba_rank_key(senior_part_cheaper) < cba_rank_key(senior_full)


def test_evaluate_seniority_bypass_when_senior_unavailable() -> None:
    senior = EmployeeProfile(
        "emp-senior",
        "Jordan Patel",
        1.0,
        {"qual-mlt"},
        seniority_hours=9200.0,
        base_hourly_rate=40.0,
    )
    junior = EmployeeProfile(
        "emp-junior",
        "Avery Miller",
        0.8,
        {"qual-mlt"},
        seniority_hours=6800.0,
        base_hourly_rate=40.0,
    )
    bypass = evaluate_seniority_bypass(
        qualified_profiles=[senior, junior],
        eligible_ids={"emp-junior"},
        selected=junior,
        ineligible_reasons={
            "emp-senior": "would violate 11h rest before Morning after Evening/Night (8.0h gap)"
        },
    )
    assert bypass is not None
    assert bypass.most_senior_qualified_id == "emp-senior"
    assert bypass.requires_manual_justification is False
    assert "unavailable" in bypass.justification


def test_evaluate_seniority_bypass_requires_manual_when_senior_eligible() -> None:
    senior = EmployeeProfile(
        "emp-senior",
        "Jordan Patel",
        1.0,
        {"qual-mlt"},
        seniority_hours=9200.0,
        base_hourly_rate=40.0,
    )
    junior = EmployeeProfile(
        "emp-junior",
        "Avery Miller",
        0.8,
        {"qual-mlt"},
        seniority_hours=6800.0,
        base_hourly_rate=26.0,
    )
    bypass = evaluate_seniority_bypass(
        qualified_profiles=[senior, junior],
        eligible_ids={"emp-senior", "emp-junior"},
        selected=junior,
    )
    assert bypass is not None
    assert bypass.requires_manual_justification is True
    assert bypass.most_senior_eligible is True
