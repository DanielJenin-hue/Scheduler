from __future__ import annotations

import pytest

from portage_fixtures import portage_generate_kwargs


@pytest.fixture
def portage_block_kwargs():
    return portage_generate_kwargs()
