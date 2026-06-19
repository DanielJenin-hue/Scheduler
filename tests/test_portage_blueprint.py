from lab_scheduler.simulation.portage_blueprint import (
    PORTAGE_LINE_BLUEPRINT,
    PORTAGE_MLA_LINE_COUNT,
    PORTAGE_MLT_LINE_COUNT,
    PORTAGE_ROSTER_SIZE,
    build_portage_blueprint_roster,
)
from lab_scheduler.staff.lifecycle import PORTAGE_WEEKLY_HOUR_TIERS, bulk_target_weekly_hours_options


def test_portage_blueprint_line_counts() -> None:
    assert len(PORTAGE_LINE_BLUEPRINT) == PORTAGE_ROSTER_SIZE == 25
    mlt = [line for line in PORTAGE_LINE_BLUEPRINT if line[0] == "MLT"]
    mla = [line for line in PORTAGE_LINE_BLUEPRINT if line[0] == "MLA"]
    assert len(mlt) == PORTAGE_MLT_LINE_COUNT == 13
    assert len(mla) == PORTAGE_MLA_LINE_COUNT == 12
    assert sum(1 for _role, _line, fte in mlt if fte == 1.0 and _line == "D/N") == 4
    assert sum(1 for _role, _line, fte in mlt if fte == 1.0 and _line == "D/E") == 6
    assert 0.8 not in {fte for _role, _line, fte in PORTAGE_LINE_BLUEPRINT}


def test_bulk_provision_hours_exclude_32h_tier() -> None:
    options = bulk_target_weekly_hours_options(40.0)
    assert options == PORTAGE_WEEKLY_HOUR_TIERS
    assert 32.0 not in options


def test_blueprint_roster_uses_vacant_line_names() -> None:
    roster = build_portage_blueprint_roster()
    assert roster[0].full_name == "Vacant MLT D/N - Line 01"
    assert roster[3].full_name == "Vacant MLT D/N - Line 04"
    assert roster[4].full_name == "Vacant MLT D/E - Line 01"
    assert roster[9].full_name == "Vacant MLT D/E - Line 06"
    assert roster[12].full_name == "Vacant MLT D/E - Line 09"
    assert roster[13].full_name == "Vacant MLA D/E - Line 01"
    assert roster[17].full_name == "Vacant MLA D/E - Line 05"
    assert roster[18].full_name == "Vacant MLA D/N - Line 01"
    assert roster[-1].full_name == "Vacant MLA D/E - Line 08"
