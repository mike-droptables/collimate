"""P5 local/CI dependency tool identities and accepted server runtime policy."""
from pathlib import Path
import tomllib

from packaging.requirements import Requirement
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_dev_tools_and_workflow_pins_match_F1096_F1976():
    project = tomllib.loads((ROOT / 'api/pyproject.toml').read_text())
    dev = {Requirement(req).name: Requirement(req) for req in project['dependency-groups']['dev']}
    assert str(dev['ruff'].specifier) == '==0.17.2'
    assert str(dev['pyright'].specifier) == '==1.1.414'
    workflow = yaml.safe_load((ROOT / '.github/workflows/test.yml').read_text())
    lint = workflow['jobs']['api-lint']['steps']
    assert next(step for step in lint if step.get('name') == 'Install ruff')['run'] == 'pip install ruff==0.17.2'
    types = workflow['jobs']['api-typecheck']['steps']
    pyright = next(step for step in types if step.get('name') == 'Pyright')
    assert pyright['run'] == 'uv run --locked pyright api' and 'working-directory' not in pyright


def test_single_websocket_requirement_and_plain_uvicorn_F1864():
    project = tomllib.loads((ROOT / 'api/pyproject.toml').read_text())['project']
    requirements = [Requirement(req) for req in project['dependencies']]
    websocket = [req for req in requirements if req.name == 'websockets']
    assert len(websocket) == 1 and str(websocket[0].specifier) == '<18,>=17'
    uvicorn = next(req for req in requirements if req.name == 'uvicorn')
    assert not uvicorn.extras
    assert any(req.name == 'pydantic' for req in requirements)
