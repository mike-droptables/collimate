"""D24 Guide orientation is grounded in accepted handler capabilities."""
from pathlib import Path
import yaml

from tests._workspace import GENERATORS


ROOT=Path(__file__).resolve().parents[1]
GUIDE=GENERATORS / 'agent/agent/configs/guide'
TOOLS={'list_handlers','handler_skills','validate_file'}


def test_guide_skill_uses_two_references_and_catalog_tools_D24():
    text=(GUIDE/'files/_skills/collimate-guide/SKILL.md').read_text()
    assert all(name in text for name in TOOLS)
    assert 'references/collimate-overview.md' in text and 'references/why-collimate.md' in text
    assert 'accepted' in text and 'unavailable' in text
    assert not any(name in text for name in ['list_generators','list_renderers','list_runners','create_runner','call_runner'])
    assert len(list((GUIDE/'files/_skills/collimate-guide/references').glob('*.md')))==2


def test_guide_preset_and_overview_ground_capability_claims_D24():
    description=yaml.safe_load((GUIDE/'manifest.config_yaml').read_text())['description']
    assert 'handler catalog' in description
    assert 'installed generators' not in description and 'demonstrating' not in description
    text=(ROOT/'shared/docs/collimate-overview.md').read_text()
    assert all(name in text for name in TOOLS)
    assert 'accepted' in text and 'does not list saved runners' in text
    assert 'Cross-cell access' not in text


def test_guide_orients_on_the_one_capabilities_stack():
    """The overview and the Guide skill describe one Capabilities stack whose
    Manage face edits runners and whose handler cards review builds; the
    runners panel only runs."""
    text=(ROOT/'shared/docs/collimate-overview.md').read_text()
    assert '| **Capabilities** | The one stack that holds every generator, runner, renderer and handler' in text
    assert 'Nothing runs on the Capabilities stack.' in text
    assert 'The panel only runs runners' in text and "Manage's Runners group" in text
    assert '**Build…** opens a work stack' in text and '**Use these skills**' in text
    skill=(GUIDE/'files/_skills/collimate-guide/SKILL.md').read_text()
    assert 'Capabilities stack' in skill and 'accepts handler builds from their cards' in skill


def test_guide_catalog_tools_exist_without_file_or_web_tools_D24(native_bridge):
    from _agent_shared.workspace_tools import tool_entries
    entries={name:fn for name,kind,main,source,fn in tool_entries(enable_file_tools=False,websearch=False)}
    assert set(entries)==TOOLS
    handler={'id':'guide-example','types':[{'ext':'.sample','surface':False}]}
    native_bridge.stub_handlers([handler],skills={'guide-example':{'guide-example--author/SKILL.md':'# Accepted sample authoring'}})
    assert entries['list_handlers']()==[handler]
    assert entries['handler_skills']('.sample')=={'guide-example--author/SKILL.md':'# Accepted sample authoring'}


def test_guide_validation_reports_structure_without_mutating_file_D24(native_bridge):
    import gen
    from _agent_shared.workspace_tools import tool_entries
    entries={name:fn for name,kind,main,source,fn in tool_entries(enable_file_tools=False,websearch=False)}
    gen.update_file('draft.sample','invalid sample')
    native_bridge.stub_verifier('.sample',lambda content:{'valid':False,'error':'missing title'})
    before=set(native_bridge.tracked_writes)
    assert entries['validate_file']('draft.sample')=={'valid':False,'error':'missing title'}
    assert gen.read_file('draft.sample')=='invalid sample'
    assert set(native_bridge.tracked_writes)==before
