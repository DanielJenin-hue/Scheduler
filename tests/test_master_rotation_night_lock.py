"""D/N master night blocks must not be reshuffled by equity passes."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy

from lab_scheduler.scheduling.auto_generate import (
    _equity_allow_frozen_alternate_swap,
    _master_rotation_owns_alternate_band,
)


def test_dn_nights_are_master_owned() -> None:
    assert _master_rotation_owns_alternate_band("D/N") is True
    assert _master_rotation_owns_alternate_band("D/E") is False


def test_equity_may_not_swap_frozen_dn_nights() -> None:
    assert _equity_allow_frozen_alternate_swap("D/N", "N") is False
    assert _equity_allow_frozen_alternate_swap("D/E", "E") is True
