"""Drag-fill palette token rules (must match grid JS)."""

from __future__ import annotations

from scripts.app import EMPTY_SHIFT_DISPLAY, _drag_fill_palette_tokens_for_contract_line


def test_drag_fill_tokens_for_de_line() -> None:
    assert _drag_fill_palette_tokens_for_contract_line("D/E") == (
        EMPTY_SHIFT_DISPLAY,
        "D",
        "E",
    )


def test_drag_fill_tokens_for_dn_line() -> None:
    assert _drag_fill_palette_tokens_for_contract_line("D/N") == (
        EMPTY_SHIFT_DISPLAY,
        "D",
        "N",
    )


def test_drag_fill_grid_html_wires_contract_aware_palette() -> None:
    from datetime import date
    from unittest.mock import patch

    import pandas as pd

    from scripts.app import _build_master_schedule_grid_html

    view_dates = [date(2026, 6, 1)]
    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = {}
        html = _build_master_schedule_grid_html(
            pd.DataFrame(
                [
                    {
                        "Employee": "Vacant MLT D/E - Line 01",
                        "employee_id": "line-de",
                        "contract_line_type": "D/E",
                        date(2026, 6, 1).isoformat(): "D",
                    },
                    {
                        "Employee": "Vacant MLT D/N - Line 02",
                        "employee_id": "line-dn",
                        "contract_line_type": "D/N",
                        date(2026, 6, 1).isoformat(): "D",
                    },
                ]
            ),
            view_dates,
            period_id="period-1",
            edit_mode=True,
        )

    assert "allowedTokensForSelection" in html
    assert "bindPaletteToken" in html
    assert "suppressPaletteDismissUntil" in html
    assert "allowedWorkedTokensForLine" in html
    assert "lab-marquee-select" in html
    assert "applyRectSelection" in html
    assert "rectsIntersect" in html
