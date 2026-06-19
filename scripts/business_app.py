"""Operator Business section — revenue cockpit for Manitoba lab GTM.

Launch:
    streamlit run scripts/business_app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
SCRIPTS_DIR = ROOT / "scripts"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import streamlit as st  # noqa: E402

from app import DB_PATH, _connect_app_db, _ensure_demo_db  # noqa: E402
from lab_scheduler.ui.business import render_business_app  # noqa: E402

__all__ = ["main"]


def main() -> None:
    st.set_page_config(
        page_title="Port Optical · Business",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _ensure_demo_db(DB_PATH)
    conn = _connect_app_db()
    try:
        render_business_app(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
