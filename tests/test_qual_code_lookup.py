from lab_scheduler.engine.demand import (
    build_qual_code_lookup,
    infer_qual_code,
    qualification_id_to_code,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile


def test_qualification_id_to_code_maps_portage_la_to_mla() -> None:
    assert qualification_id_to_code("qual-la") == "MLA"
    assert qualification_id_to_code("qual-mlt") == "MLT"


def test_build_qual_code_lookup_includes_shift_template_qualifications() -> None:
    employee = EmployeeProfile(
        "emp-mla",
        "MLA Tech",
        1.0,
        {"qual-la"},
        contract_line_type="D/E",
    )
    lookup = build_qual_code_lookup(
        [employee],
        {"shift-evening": {"qual-la", "qual-mlt"}},
    )
    assert lookup["qual-la"] == "MLA"
    assert lookup["qual-mlt"] == "MLT"
    assert infer_qual_code(employee, qual_codes=lookup) == "MLA"
