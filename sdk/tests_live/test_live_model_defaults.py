# SPDX-License-Identifier: MIT
"""The suite's own default models are ones the providers still serve.

A retired default does not fail loudly on its own — the tests that use it skip
(see ``conftest.unserved_reason``) — so this is the one place that goes red
when a default in ``conftest._MULTIMODAL_DEFAULT`` or the engine's
``PROVIDER_DEFAULT_MODELS`` needs rotating. It reads Cerebras's public catalog
(no tokens billed), and only when a Cerebras key opts the provider in.
"""
from __future__ import annotations

import pytest

from .conftest import _MULTIMODAL_DEFAULT, cerebras_catalog, eng, has_key

pytestmark = pytest.mark.live


@pytest.mark.skipif(not has_key("cerebras"), reason="CEREBRAS_API_KEY is not set")
def test_cerebras_defaults_are_still_served():
    served = cerebras_catalog()
    if served is None:
        pytest.skip("the Cerebras public model catalog is unreachable")
    defaults = {eng.PROVIDER_DEFAULT_MODELS["cerebras"], _MULTIMODAL_DEFAULT["cerebras"]}
    assert defaults <= served, (sorted(defaults - served), sorted(served))
