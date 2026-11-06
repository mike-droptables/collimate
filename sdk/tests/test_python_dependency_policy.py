# SPDX-License-Identifier: MIT
"""P5 dependency policy and offline provider/tool behavior."""
import email.parser
from pathlib import Path
import tomllib

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]


def test_sdk_requirements_omit_pydantic_F1304():
    """The SDK packages need pydantic never; the one distribution (the server)
    does, and its metadata resource is the root project's dependencies."""
    from collimate.shared import SDK_REQUIREMENTS
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    assert 'pydantic' not in {Requirement(req).name for req in SDK_REQUIREMENTS}
    assert set(SDK_REQUIREMENTS) <= set(project['dependencies'])
    metadata = email.parser.Parser().parsestr(
        (ROOT / 'sdk/src/collimate/_sdk_metadata/METADATA').read_text())
    core = [Requirement(req) for req in metadata.get_all('Requires-Dist', [])
            if Requirement(req).marker is None]
    assert {str(req) for req in core} == {str(Requirement(req)) for req in project['dependencies']}


def test_ruff_is_pinned_and_no_model_sdk_is_a_dev_dependency_F1866_F1976():
    # The pydantic-ai harness's model SDK lives in its own pinned environment
    # (adapter_artifacts.PYTHON_ADAPTERS), never the SDK's.
    groups = tomllib.loads((ROOT / 'pyproject.toml').read_text())['dependency-groups']
    dev = {Requirement(req).name: Requirement(req) for req in groups['dev']}
    assert not {'pydantic-ai', 'pydantic-ai-slim', 'pydantic-ai-harness'} & dev.keys()
    assert str(dev['ruff'].specifier) == '==0.17.2'
