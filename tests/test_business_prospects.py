import sqlite3
from pathlib import Path

import pytest

from lab_scheduler.business.discovery import (
    DEFAULT_FACILITY_DATASET,
    compute_icp_score,
    discover_manitoba_prospects,
    list_scored_manitoba_facilities,
    purge_excluded_prospects,
    score_facility_record,
)
from lab_scheduler.business.email_templates import (
    default_outreach_sender_name,
    generate_outreach_email,
    validate_first_touch_draft,
)
from lab_scheduler.business.models import ProspectStatus, ensure_business_prospects_schema
from lab_scheduler.business.prospect_service import (
    ProspectServiceError,
    create_prospect,
    generate_email_preview,
    get_prospect,
    list_prospects,
    proceed_with_client,
    update_prospect_status,
)
from lab_scheduler.rsi.prospector import RegionalFacilityRecord


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE tenants (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          slug TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL,
          subscription_status TEXT NOT NULL DEFAULT 'trial',
          stripe_customer_id TEXT,
          trial_ends_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE qualifications (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          code TEXT NOT NULL,
          display_name TEXT NOT NULL,
          description TEXT,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
        CREATE TABLE shift_templates (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          code TEXT NOT NULL,
          name TEXT NOT NULL,
          start_time TEXT NOT NULL,
          end_time TEXT NOT NULL,
          duration_minutes INTEGER NOT NULL,
          crosses_midnight INTEGER NOT NULL DEFAULT 0,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE shift_template_qualifications (
          tenant_id TEXT NOT NULL,
          shift_template_id TEXT NOT NULL,
          qualification_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY (tenant_id, shift_template_id, qualification_id)
        );
        CREATE TABLE employees (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          employee_code TEXT,
          first_name TEXT NOT NULL,
          last_name TEXT NOT NULL,
          hire_date TEXT NOT NULL,
          fte REAL NOT NULL,
          base_hourly_rate REAL NOT NULL DEFAULT 40.0,
          seniority_hours REAL NOT NULL DEFAULT 0.0,
          contract_line_type TEXT,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE schedule_periods (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          label TEXT NOT NULL,
          period_start TEXT NOT NULL,
          period_end_inclusive TEXT NOT NULL,
          week_count INTEGER NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    ensure_business_prospects_schema(conn)
    return conn


def _sample_facility() -> RegionalFacilityRecord:
    return RegionalFacilityRecord(
        facility_id="MB-TEST-1",
        facility_name="Test Regional Lab",
        region="Prairies",
        state_province="MB",
        annual_test_volume=900_000,
        mlt_fte=12.0,
        mla_fte=10.0,
    )


def test_compute_icp_score_is_bounded() -> None:
    score = compute_icp_score(
        deployment_score=250.0,
        annual_test_volume=2_000_000,
        mlt_fte=30.0,
        mla_fte=25.0,
    )
    assert 0 <= score <= 100


def test_score_facility_record_derives_pain_signals() -> None:
    facility = _sample_facility()
    report, icp, pain = score_facility_record(facility)
    assert report.facility_id == "MB-TEST-1"
    assert icp > 0
    assert any("Manitoba" in signal for signal in pain)
    assert any("breakroom" in signal.lower() for signal in pain)


def test_discover_manitoba_prospects_from_csv(tmp_path: Path) -> None:
    dataset = tmp_path / "facilities.csv"
    dataset.write_text(
        "facility_id,facility_name,region,state_province,annual_test_volume,mlt_fte,mla_fte\n"
        "MB-A,Alpha Lab,Prairies,MB,900000,10,8\n"
        "ON-B,Beta Lab,Ontario,ON,900000,10,8\n"
        "MB-C,Small Lab,Prairies,MB,100000,2,1\n",
        encoding="utf-8",
    )
    conn = _memory_db()
    result = discover_manitoba_prospects(conn, dataset_path=dataset)
    assert result.created == 2
    assert result.updated == 0
    assert {p.facility for p in result.prospects} == {"Alpha Lab", "Small Lab"}
    assert all(p.province == "MB" for p in result.prospects)


def test_create_and_list_prospects() -> None:
    conn = _memory_db()
    created = create_prospect(
        conn,
        facility="Health Sciences Centre Winnipeg",
        facility_id="MB-WPG-HSC",
        icp_score=82,
        pain_signals=["Union fatigue rules"],
    )
    assert created.status == ProspectStatus.DISCOVERED
    rows = list_prospects(conn, min_icp_score=80)
    assert len(rows) == 1
    assert rows[0].id == created.id


def test_default_manitoba_dataset_has_expanded_pipeline() -> None:
    scored = list_scored_manitoba_facilities(DEFAULT_FACILITY_DATASET)
    assert len(scored) >= 10
    names = {facility.facility_name for facility, _report, _icp, _pain in scored}
    assert "Health Sciences Centre Winnipeg" in names
    assert "Brandon Regional Health Centre" in names
    assert "Portage Regional Health Centre" not in names


def test_purge_excluded_portage_prospect() -> None:
    conn = _memory_db()
    create_prospect(
        conn,
        facility="Portage Regional Health Centre",
        facility_id="MB-WPG-PORTAGE",
        icp_score=100,
    )
    create_prospect(
        conn,
        facility="St. Boniface Hospital",
        facility_id="MB-WPG-STB",
        icp_score=90,
    )
    removed = purge_excluded_prospects(conn)
    assert removed == 1
    facilities = {p.facility for p in list_prospects(conn)}
    assert "Portage Regional Health Centre" not in facilities
    assert "St. Boniface Hospital" in facilities


def test_generate_email_preview_persists_draft_and_status() -> None:
    conn = _memory_db()
    created = create_prospect(
        conn,
        facility="St. Boniface Hospital",
        contact_name="Alex Manager",
        pain_signals=["High test volume increases scheduling leakage and OT risk"],
    )
    draft = generate_email_preview(conn, created.id)
    assert "St. Boniface Hospital" in draft.subject
    assert "Alex" in draft.body
    assert "breakroom" in draft.body.lower()

    refreshed = get_prospect(conn, created.id)
    assert refreshed.email_draft_subject == draft.subject
    assert refreshed.email_draft_body == draft.body
    assert refreshed.status == ProspectStatus.PREVIEWED


def test_generate_outreach_email_is_managed_first_professional() -> None:
    prospect = create_prospect(
        _memory_db(),
        facility="Selkirk Regional Lab",
        contact_name="Jordan Lee",
        pain_signals=["High test volume increases scheduling leakage and OT risk"],
    )
    draft = generate_outreach_email(prospect)
    lowered = draft.body.lower()
    assert "managed" in lowered
    assert "$800" in draft.body or "800" in draft.body
    assert "14-day trial" not in lowered
    assert "sample breakroom" not in lowered
    assert "what we deliver for managers" not in lowered
    assert "posting season" in lowered or "evenings, nights" in lowered
    assert "walkthrough" in lowered or "walkthrough times" in lowered
    assert 'reply with "yes — [week] works"' in lowered
    assert "!!!" not in draft.body
    assert "urgent" not in lowered
    assert "Selkirk Regional Lab" in draft.subject or "selkirk" in draft.subject.lower()
    assert "Jordan" in draft.body
    assert "portage" not in lowered
    assert len(draft.body.split()) <= 120
    assert not validate_first_touch_draft(draft.body, draft.subject)


def test_boundary_trails_first_touch_no_portage_or_compliance_bullet() -> None:
    prospect = create_prospect(
        _memory_db(),
        facility="Boundary Trails Health Centre",
        facility_id="MB-MOR-BTHC",
        pain_signals=[
            "Manitoba union fatigue and rest rules require audit-ready schedules",
            "Managers need breakroom-ready HTML export, not another weekend in Excel",
        ],
    )
    draft = generate_outreach_email(prospect)
    lowered = draft.body.lower()
    assert "portage" not in lowered
    assert "hello," not in lowered.splitlines()[0]
    assert "boundary trails" in lowered
    assert "manitoba union fatigue" not in lowered
    assert draft.body.strip().endswith(default_outreach_sender_name())
    assert "—" in draft.body
    assert len(draft.body.split()) <= 120


def test_validate_first_touch_draft_flags_slop() -> None:
    warnings = validate_first_touch_draft(
        "Hello,\n\n- bullet one\n- bullet two\n\nDan — Portage Lab Staffing",
        "Test",
    )
    assert any("Hello" in w for w in warnings)
    assert any("Portage" in w for w in warnings)
    assert any("Bullet" in w for w in warnings)


def test_default_outreach_sender_name_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAB_OUTREACH_SENDER_NAME", "Dan — Manitoba lab scheduling")
    assert default_outreach_sender_name() == "Dan — Manitoba lab scheduling"


def test_proceed_with_client_creates_tenant() -> None:
    conn = _memory_db()
    created = create_prospect(
        conn,
        facility="Brandon Regional Lab",
        facility_id="MB-BDN-1",
        icp_score=70,
    )
    result = proceed_with_client(conn, created.id, create_tenant=True)
    assert result.tenant_created is True
    assert result.prospect.status == ProspectStatus.ACTIVE_CLIENT
    assert result.prospect.tenant_id == result.tenant_id

    tenant = conn.execute(
        "SELECT name FROM tenants WHERE id = ?",
        (result.tenant_id,),
    ).fetchone()
    assert tenant is not None
    assert tenant[0] == "Brandon Regional Lab"


def test_proceed_with_client_links_existing_tenant() -> None:
    conn = _memory_db()
    conn.execute(
        """
        INSERT INTO tenants (
          id, name, slug, status, subscription_status,
          stripe_customer_id, trial_ends_at, created_at, updated_at
        ) VALUES (
          'tenant-existing', 'Existing Lab', 'existing-lab', 'active', 'trial',
          NULL, '2026-07-01', '2026-06-01T00:00:00Z', '2026-06-01T00:00:00Z'
        )
        """
    )
    conn.commit()
    created = create_prospect(conn, facility="Linked Lab")
    result = proceed_with_client(conn, created.id, tenant_id="tenant-existing")
    assert result.tenant_created is False
    assert result.tenant_id == "tenant-existing"
    assert result.prospect.tenant_id == "tenant-existing"


def test_update_prospect_status_blocks_invalid_transition() -> None:
    conn = _memory_db()
    created = create_prospect(conn, facility="Declined Lab")
    update_prospect_status(conn, created.id, ProspectStatus.DECLINED)
    with pytest.raises(ProspectServiceError):
        update_prospect_status(conn, created.id, ProspectStatus.CONTACTED)
