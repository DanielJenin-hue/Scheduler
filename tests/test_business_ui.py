"""Smoke tests for Business Streamlit UI helpers."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from lab_scheduler.business.models import ensure_business_prospects_schema
from lab_scheduler.business.prospect_service import create_prospect
from lab_scheduler.ui.business.components import (
    MANAGED_BLOCK_CAD,
    MRR_TARGET_CAD,
    PRO_SEAT_MRR_CAD,
    render_prospect_card_html,
)
from lab_scheduler.ui.business.helpers import (
    build_template_context,
    derive_pitch_angle,
    icp_display_score,
    load_facility_enrichment,
    merge_template_variables,
    status_label,
)
from lab_scheduler.ui.business.navigation import (
    BUSINESS_TABS,
    DEFAULT_BUSINESS_TAB,
    apply_pending_app_section,
    apply_pending_business_tab,
    request_app_section,
    request_business_tab,
    resolve_business_tab,
)
from lab_scheduler.ui.business.helpers import (
    FIRST_TOUCH_SUBJECT_VARIANT_LABELS,
    email_preview_envelope_html,
    first_touch_subject,
)
from lab_scheduler.business.models import ProspectStatus


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def test_icp_display_score_scales_to_25() -> None:
    score, maximum = icp_display_score(80)
    assert maximum == 25
    assert score == 20


def test_status_label_maps_discovered_to_new() -> None:
    assert status_label(ProspectStatus.DISCOVERED) == "New"
    assert status_label(ProspectStatus.DECLINED) == "Passed"


def test_merge_template_variables() -> None:
    merged = merge_template_variables("Hello {{first_name}} at {{facility_name}}", {
        "first_name": "Alex",
        "facility_name": "St. Boniface Hospital",
    })
    assert merged == "Hello Alex at St. Boniface Hospital"


def test_build_template_context_from_prospect() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_business_prospects_schema(conn)
    prospect = create_prospect(
        conn,
        facility="St. Boniface Hospital",
        facility_id="MB-WPG-STB",
        contact_name="Alex Manager",
        pain_signals=["Rotation complexity"],
    )
    enrichment = load_facility_enrichment(prospect)
    pitch = derive_pitch_angle(prospect, enrichment)
    context = build_template_context(
        prospect,
        enrichment,
        sender_name="Port Optical",
        pitch_angle=pitch,
    )
    assert context["first_name"] == "Alex"
    assert "St. Boniface" in context["facility_short_name"]
    assert enrichment is not None
    assert "tests/yr" in context["annual_test_volume"]


def test_revenue_plan_constants_match_north_star() -> None:
    assert MRR_TARGET_CAD == 2000
    assert PRO_SEAT_MRR_CAD == 299
    assert MANAGED_BLOCK_CAD == 800


def test_business_app_module_importable() -> None:
    script_path = SCRIPTS / "business_app.py"
    assert script_path.is_file()
    spec = importlib.util.spec_from_file_location("business_app", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["business_app"] = module
    spec.loader.exec_module(module)
    assert callable(module.main)


def test_request_business_tab_queues_pending_navigation() -> None:
    state: dict[str, object] = {"business_tab": "Pipeline"}
    request_business_tab(state, "Email Preview")
    assert state["business_tab_pending"] == "Email Preview"
    assert state["business_tab"] == "Pipeline"


def test_apply_pending_business_tab_before_widget_render() -> None:
    state: dict[str, object] = {
        "business_tab": "Pipeline",
        "business_tab_pending": "Email Preview",
    }
    active = apply_pending_business_tab(state)
    assert active == "Email Preview"
    assert state["business_tab"] == "Email Preview"
    assert "business_tab_pending" not in state


def test_apply_pending_rejects_invalid_tab() -> None:
    state: dict[str, object] = {
        "business_tab": "Prospects",
        "business_tab_pending": "Not A Tab",
    }
    active = apply_pending_business_tab(state)
    assert active == "Prospects"


def test_resolve_business_tab_falls_back_to_default() -> None:
    assert resolve_business_tab({"business_tab": "bogus"}) == DEFAULT_BUSINESS_TAB


def test_request_business_tab_rejects_unknown_label() -> None:
    with pytest.raises(ValueError, match="Unknown business tab"):
        request_business_tab({}, "Scheduling")


def test_business_tabs_cover_revenue_flow() -> None:
    assert BUSINESS_TABS == (
        "Pipeline",
        "Prospects",
        "Inbox",
        "Email Preview",
        "Client Onboarding",
    )


def test_request_app_section_queues_pending_navigation() -> None:
    state: dict[str, object] = {"app_section": "Scheduling"}
    request_app_section(state, "Business")
    assert state["app_section_pending"] == "Business"
    assert state["app_section"] == "Scheduling"


def test_apply_pending_app_section_before_widget_render() -> None:
    state: dict[str, object] = {
        "app_section": "Scheduling",
        "app_section_pending": "Business",
    }
    active = apply_pending_app_section(state)
    assert active == "Business"
    assert state["app_section"] == "Business"
    assert "app_section_pending" not in state


def test_first_touch_subject_variants_match_psychology_brief() -> None:
    facility = "Selkirk Regional Lab"
    assert first_touch_subject(facility_name=facility, variant="a") == (
        f"{facility} — staff schedule before posting season?"
    )
    assert first_touch_subject(facility_name=facility, variant="b") == (
        f"{facility} rotation — one question before you post"
    )
    assert first_touch_subject(facility_name=facility, variant="c") == (
        f"Quick question — MLT lines at {facility}"
    )
    assert set(FIRST_TOUCH_SUBJECT_VARIANT_LABELS.values()) == {"a", "b", "c"}


def test_email_preview_envelope_escapes_and_includes_headers() -> None:
    html_out = email_preview_envelope_html(
        to='test@example.com',
        subject='Hello <world>',
        body='Line one\nLine two',
    )
    assert "test@example.com" in html_out
    assert "Hello &lt;world&gt;" in html_out
    assert "Line one" in html_out
    assert "biz-email-envelope" in html_out


def test_email_preview_envelope_empty_to_shows_prompt_not_placeholder() -> None:
    html_out = email_preview_envelope_html(to="", subject="Hi", body="Body")
    assert "lab.manager@example.com" not in html_out
    assert "(add recipient email)" in html_out


def test_prospect_card_html_starts_with_tag_not_indent() -> None:
    """Indented HTML is rendered as a markdown code block in Streamlit."""
    conn = sqlite3.connect(":memory:")
    ensure_business_prospects_schema(conn)
    prospect = create_prospect(
        conn,
        facility="St. Boniface Hospital",
        facility_id="MB-WPG-STB",
        contact_name="Alex Manager",
        pain_signals=["Rotation complexity"],
    )
    enrichment = load_facility_enrichment(prospect)
    card_html = render_prospect_card_html(prospect, enrichment, compact=True)
    assert card_html.startswith("<div")
    assert '<div class="biz-card"' in card_html
    assert "St. Boniface Hospital" in card_html
    assert "&lt;div" not in card_html.split("St. Boniface Hospital")[0]
