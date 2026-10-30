# SPDX-License-Identifier: MIT
"""P5 dependency policy and offline provider/tool behavior."""
import email.parser
from pathlib import Path
import tomllib

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def test_core_metadata_omits_pydantic_F1304():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    assert 'pydantic' not in {Requirement(req).name for req in project['dependencies']}
    metadata = email.parser.Parser().parsestr(
        (ROOT / 'src/collimate/_sdk_metadata/METADATA').read_text())
    core = [Requirement(req) for req in metadata.get_all('Requires-Dist', [])
            if Requirement(req).marker is None]
    assert 'pydantic' not in {req.name for req in core}
    assert {str(req) for req in core} == {str(Requirement(req)) for req in project['dependencies']}


def test_ruff_is_pinned_and_no_model_sdk_is_a_dev_dependency_F1866_F1976():
    # The pydantic-ai harness's model SDK lives in its own pinned environment
    # (adapter_artifacts.PYTHON_ADAPTERS), never the SDK's.
    groups = tomllib.loads((ROOT / 'pyproject.toml').read_text())['dependency-groups']
    dev = {Requirement(req).name: Requirement(req) for req in groups['dev']}
    assert not {'pydantic-ai', 'pydantic-ai-slim', 'pydantic-ai-harness'} & dev.keys()
    assert str(dev['ruff'].specifier) == '==0.17.2'
