"""Business section tab navigation — safe with Streamlit keyed widgets.

Streamlit forbids mutating ``session_state[key]`` after a widget with that key is
drawn. Use :func:`request_business_tab` from button handlers, then
:func:`apply_pending_business_tab` at the start of each render **before**
``st.radio(..., key="business_tab")``.
"""

from __future__ import annotations

from typing import Mapping, MutableMapping, Sequence

BUSINESS_TABS: tuple[str, ...] = (
    "Pipeline",
    "Prospects",
    "Inbox",
    "Email Preview",
    "Client Onboarding",
)
DEFAULT_BUSINESS_TAB = "Pipeline"
_PENDING_KEY = "business_tab_pending"
_TAB_STATE_KEY = "business_tab"

APP_SECTIONS: tuple[str, ...] = ("Scheduling", "Business")
DEFAULT_APP_SECTION = "Scheduling"
_PENDING_APP_SECTION_KEY = "app_section_pending"
_APP_SECTION_STATE_KEY = "app_section"
APP_SECTION_STATE_KEY = _APP_SECTION_STATE_KEY

__all__ = [
    "APP_SECTIONS",
    "APP_SECTION_STATE_KEY",
    "BUSINESS_TABS",
    "DEFAULT_APP_SECTION",
    "DEFAULT_BUSINESS_TAB",
    "apply_pending_app_section",
    "apply_pending_business_tab",
    "is_valid_business_tab",
    "request_app_section",
    "request_business_tab",
    "resolve_business_tab",
]


def is_valid_business_tab(tab: str, *, tabs: Sequence[str] = BUSINESS_TABS) -> bool:
    return tab in tabs


def request_business_tab(session_state: MutableMapping[str, object], tab: str) -> None:
    """Queue a tab switch for the next render (before the tab widget is created)."""

    if not is_valid_business_tab(tab):
        raise ValueError(f"Unknown business tab: {tab!r}")
    session_state[_PENDING_KEY] = tab


def apply_pending_business_tab(
    session_state: MutableMapping[str, object],
    *,
    tabs: Sequence[str] = BUSINESS_TABS,
    default: str = DEFAULT_BUSINESS_TAB,
) -> str:
    """Apply queued tab navigation and return the active tab label."""

    session_state.setdefault(_TAB_STATE_KEY, default)
    pending = session_state.pop(_PENDING_KEY, None)
    if pending is not None and is_valid_business_tab(str(pending), tabs=tabs):
        session_state[_TAB_STATE_KEY] = pending
    current = str(session_state[_TAB_STATE_KEY])
    if not is_valid_business_tab(current, tabs=tabs):
        session_state[_TAB_STATE_KEY] = default
        current = default
    return current


def resolve_business_tab(session_state: Mapping[str, object]) -> str:
    """Read the current tab without applying pending navigation."""

    current = str(session_state.get(_TAB_STATE_KEY, DEFAULT_BUSINESS_TAB))
    return current if is_valid_business_tab(current) else DEFAULT_BUSINESS_TAB


def request_app_section(session_state: MutableMapping[str, object], section: str) -> None:
    """Queue Scheduling/Business navigation before the section radio renders."""

    if section not in APP_SECTIONS:
        raise ValueError(f"Unknown app section: {section!r}")
    session_state[_PENDING_APP_SECTION_KEY] = section


def apply_pending_app_section(
    session_state: MutableMapping[str, object],
    *,
    default: str = DEFAULT_APP_SECTION,
) -> str:
    """Apply queued operator section navigation before ``st.radio(key=app_section)``."""

    session_state.setdefault(_APP_SECTION_STATE_KEY, default)
    pending = session_state.pop(_PENDING_APP_SECTION_KEY, None)
    if pending in APP_SECTIONS:
        session_state[_APP_SECTION_STATE_KEY] = pending
    current = str(session_state.get(_APP_SECTION_STATE_KEY, default))
    if current not in APP_SECTIONS:
        session_state[_APP_SECTION_STATE_KEY] = default
        current = default
    return current
