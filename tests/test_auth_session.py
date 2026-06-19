import sqlite3

import pytest

from lab_scheduler.auth.session import authenticate_user, seed_default_accounts


@pytest.fixture(autouse=True)
def _enable_demo_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAB_ALLOW_DEMO_ACCOUNTS", "1")


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE tenants (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL, status TEXT NOT NULL
        );
        INSERT INTO tenants VALUES
          ('tenant-northstar-lab', 'Northstar Medical Laboratory', 'northstar-lab', 'active'),
          ('tenant-southbridge-health', 'Southbridge Community Health Laboratory', 'southbridge-health', 'active');
        CREATE TABLE tenant_user_accounts (
          id TEXT PRIMARY KEY,
          username TEXT NOT NULL UNIQUE,
          password_salt TEXT NOT NULL,
          password_hash TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          display_name TEXT,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    seed_default_accounts(conn)
    return conn


def test_authenticate_northstar_admin() -> None:
    conn = _memory_db()
    session = authenticate_user(conn, username="northstar_admin", password="labpass123")
    assert session is not None
    assert session.tenant_id == "tenant-northstar-lab"


def test_authenticate_southbridge_admin() -> None:
    conn = _memory_db()
    session = authenticate_user(conn, username="southbridge_admin", password="healthpass456")
    assert session is not None
    assert session.tenant_id == "tenant-southbridge-health"


def test_authenticate_rejects_invalid_password() -> None:
    conn = _memory_db()
    assert authenticate_user(conn, username="northstar_admin", password="wrong") is None


def test_ensure_demo_account_credentials_resets_bad_password() -> None:
    conn = _memory_db()
    conn.execute(
        """
        UPDATE tenant_user_accounts
        SET password_hash = 'deadbeef', password_salt = 'bad'
        WHERE username = 'northstar_admin'
        """
    )
    conn.commit()
    assert authenticate_user(conn, username="northstar_admin", password="labpass123") is None

    from lab_scheduler.auth.session import ensure_demo_account_credentials

    ensure_demo_account_credentials(conn)
    session = authenticate_user(conn, username="northstar_admin", password="labpass123")
    assert session is not None
    assert session.tenant_id == "tenant-northstar-lab"


def test_tenant_isolation_mapping() -> None:
    conn = _memory_db()
    north = authenticate_user(conn, username="northstar_admin", password="labpass123")
    south = authenticate_user(conn, username="southbridge_admin", password="healthpass456")
    assert north is not None and south is not None
    assert north.tenant_id != south.tenant_id
