from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence, Tuple

PBKDF2_ITERATIONS = 100_000

DemoAccount = Tuple[str, str, str, str]

# Dev-only defaults; overridden by LAB_DEMO_* env vars when LAB_ALLOW_DEMO_ACCOUNTS=1.
_DEV_DEMO_NORTHSTAR_PASSWORD = "labpass123"
_DEV_DEMO_SOUTHBRIDGE_PASSWORD = "healthpass456"


def demo_accounts_enabled() -> bool:
    """True when bundled demo logins may be seeded (local dev / demo app only)."""
    raw = os.environ.get("LAB_ALLOW_DEMO_ACCOUNTS", "")
    return raw.strip().lower() in {"1", "true", "yes"}


def default_test_accounts() -> Sequence[DemoAccount]:
    """Return demo accounts when LAB_ALLOW_DEMO_ACCOUNTS=1; empty tuple in production."""
    if not demo_accounts_enabled():
        return ()
    north_pw = os.environ.get("LAB_DEMO_NORTHSTAR_PASSWORD", _DEV_DEMO_NORTHSTAR_PASSWORD)
    south_pw = os.environ.get("LAB_DEMO_SOUTHBRIDGE_PASSWORD", _DEV_DEMO_SOUTHBRIDGE_PASSWORD)
    return (
        ("northstar_admin", north_pw, "tenant-northstar-lab", "Northstar Administrator"),
        ("southbridge_admin", south_pw, "tenant-southbridge-health", "Southbridge Administrator"),
    )


# Back-compat alias for imports; empty unless LAB_ALLOW_DEMO_ACCOUNTS=1.
DEFAULT_TEST_ACCOUNTS = default_test_accounts()


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    account_id: str
    username: str
    tenant_id: str
    tenant_name: str
    tenant_slug: str
    display_name: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    )
    return salt, digest.hex()


def verify_password(password: str, salt: str, stored_hash: str) -> bool:
    _, computed = hash_password(password, salt)
    return secrets.compare_digest(computed, stored_hash)


def seed_default_accounts(conn: sqlite3.Connection) -> None:
    accounts = default_test_accounts()
    if not accounts:
        return

    count = conn.execute("SELECT COUNT(*) FROM tenant_user_accounts").fetchone()[0]
    if int(count) > 0:
        return

    now = _utc_now_iso()
    for username, password, tenant_id, display_name in accounts:
        salt, pwd_hash = hash_password(password)
        conn.execute(
            """
            INSERT INTO tenant_user_accounts (
              id, username, password_salt, password_hash, tenant_id,
              display_name, is_active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                f"acct-{uuid.uuid4().hex[:12]}",
                username,
                salt,
                pwd_hash,
                tenant_id,
                display_name,
                now,
                now,
            ),
        )
    conn.commit()


def ensure_demo_account_credentials(conn: sqlite3.Connection) -> None:
    """Keep bundled demo logins aligned with default_test_accounts() when enabled."""

    accounts = default_test_accounts()
    if not accounts:
        return

    now = _utc_now_iso()
    for username, password, tenant_id, display_name in accounts:
        tenant_exists = conn.execute(
            "SELECT 1 FROM tenants WHERE id = ?",
            (tenant_id,),
        ).fetchone()
        if tenant_exists is None:
            continue

        row = conn.execute(
            """
            SELECT id, password_salt, password_hash
            FROM tenant_user_accounts
            WHERE username = ? COLLATE NOCASE
            """,
            (username,),
        ).fetchone()

        if row is not None and verify_password(password, row[1], row[2]):
            continue

        salt, pwd_hash = hash_password(password)
        if row is None:
            conn.execute(
                """
                INSERT INTO tenant_user_accounts (
                  id, username, password_salt, password_hash, tenant_id,
                  display_name, is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    f"acct-{uuid.uuid4().hex[:12]}",
                    username,
                    salt,
                    pwd_hash,
                    tenant_id,
                    display_name,
                    now,
                    now,
                ),
            )
            continue

        conn.execute(
            """
            UPDATE tenant_user_accounts
            SET password_salt = ?, password_hash = ?, is_active = 1, updated_at = ?
            WHERE id = ?
            """,
            (salt, pwd_hash, now, row[0]),
        )
    conn.commit()


def authenticate_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    password: str,
) -> Optional[AuthenticatedSession]:
    row = conn.execute(
        """
        SELECT
          a.id,
          a.username,
          a.password_salt,
          a.password_hash,
          a.tenant_id,
          a.display_name,
          t.name,
          t.slug
        FROM tenant_user_accounts a
        JOIN tenants t ON t.id = a.tenant_id
        WHERE a.username = ? COLLATE NOCASE AND a.is_active = 1
        """,
        (username.strip(),),
    ).fetchone()

    if row is None:
        return None
    if not verify_password(password.strip(), row[2], row[3]):
        return None

    return AuthenticatedSession(
        account_id=row[0],
        username=row[1],
        tenant_id=row[4],
        tenant_name=row[6],
        tenant_slug=row[7],
        display_name=row[5] or row[1],
    )
