"""Options preset skills carry their current usable file format."""
import re

import pytest
import yaml

from tests._workspace import GENERATORS


PRESETS = ["axes-first", "seed-ideas", "modify-files", "challenge", "evaluate-best", "extract", "summarize", "translate"]
ROOT = GENERATORS / "agent/agent/configs"

@pytest.mark.parametrize("name", PRESETS)
def test_preset_carries_complete_options_format__D08(name):
    skill = (ROOT / name / "files/_skills" / name / "SKILL.md").read_text()
    examples = [yaml.safe_load(block) for block in re.findall(r"```yaml\n(.*?)```", skill, re.S)]
    declaration = next((item for item in examples if isinstance(item, dict) and "options" in item), None)
    assert declaration is not None, name
    from collimate.options_stack import parse_options_stack, map_options_stack
    parsed = parse_options_stack('.options_stack', yaml.safe_dump(declaration))
    paths = ['.options_stack', 'report.md', *[option['root'] + '/document.md' for option in declaration['options']]]
    plan = map_options_stack(parsed, paths)
    assert [option['files'] for option in plan.options] == [
        {'document.md': option['root'] + '/document.md'} for option in declaration['options']]
    assert plan.kept == {'report.md'} and plan.retain_source
    assert declaration["rest"] == "keep"
    assert all(set(option) == {"name", "root", "files"} and option["files"] == ["**"] for option in declaration["options"])
    assert ".options_stack" in skill
