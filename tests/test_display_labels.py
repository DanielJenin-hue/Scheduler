from lab_scheduler.scheduling.display_labels import staff_line_display_name


def test_staff_line_display_name_includes_contract_pool_for_mla_de():
    assert (
        staff_line_display_name("Vacant MLA D/E - Line 01 (320h)")
        == "MLA D/E Line 01"
    )


def test_staff_line_display_name_includes_contract_pool_for_mla_dn():
    assert (
        staff_line_display_name("Vacant MLA D/N - Line 01 (320h)")
        == "MLA D/N Line 01"
    )


def test_staff_line_display_name_includes_contract_pool_for_mlt_de():
    assert (
        staff_line_display_name("Vacant MLT D/E - Line 02 (320h)")
        == "MLT D/E Line 02"
    )


def test_staff_line_display_names_unique_across_contract_pools():
    de = staff_line_display_name("Vacant MLA D/E - Line 01 (320h)")
    dn = staff_line_display_name("Vacant MLA D/N - Line 01 (320h)")
    assert de != dn
