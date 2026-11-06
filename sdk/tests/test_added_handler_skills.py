"""P4F01 additive guidance cannot replace accepted handler behavior."""
import pytest

from tests._workspace import GENERATORS


ACCEPTED = {'verify.ts': b'export default ()=>({valid:true});',
            'skills/author/SKILL.md': b'---\nname: document-authoring\n---\nAccepted guidance'}


def test_added_guidance_is_text_and_preserves_accepted_identity_P4F01():
    from collimate.handlers import derived_hash, validate_added_skills
    files = {'derived/' + path: body for path, body in ACCEPTED.items()}
    files['skills/local/SKILL.md'] = b'---\nname: local-guidance\n---\nUse concise summaries.\n'
    files['skills/local/references/example.md'] = b'```python\nprint("text example")\n```\n'
    before = derived_hash(ACCEPTED)
    added = validate_added_skills(files, ACCEPTED)
    assert set(added) == {'skills/local/SKILL.md', 'skills/local/references/example.md'}
    assert added['skills/local/SKILL.md'].endswith('Use concise summaries.\n')
    assert derived_hash(ACCEPTED) == before


@pytest.mark.parametrize('path,body', [
    ('skills/author/SKILL.md', b'override'),
    ('skills/scripts/SKILL.md', b'Hidden executable band'),
    ('skills/derived/SKILL.md', b'Hidden derived band'),
    ('skills/local/derived/readme.md', b'override'),
    ('skills/local/verify.ts', b'export default ()=>({valid:true});'),
    ('skills/local/scripts/helper.txt', b'executable helper'),
    ('skills/local/helper.py', b'print(1)'),
    ('skills/local/helper.sh', b'exit 0'),
    ('skills/local/helper.txt', b'#!/bin/sh\nexit 0'),
    ('skills/local/image.bin', b'\xff\x00'),
    ('skills/local/references/missing.md', b'No SKILL.md'),
])
def test_added_guidance_rejects_executable_or_replacing_content_P4F01(path, body):
    from collimate.handlers import validate_added_skills
    files = {path: body}
    if path != 'skills/local/references/missing.md':
        files.setdefault('skills/local/SKILL.md', b'Local guidance')
    with pytest.raises(ValueError):
        validate_added_skills(files, ACCEPTED)


def test_previous_accepted_skill_name_stays_reserved_during_rebuild_P4F01():
    from collimate.handlers import validate_added_skills
    with pytest.raises(ValueError):
        validate_added_skills({'derived/skills/new/SKILL.md': b'New',
                              'skills/author/SKILL.md': b'Override old accepted guidance'}, ACCEPTED)


def test_frontmatter_alias_cannot_shadow_accepted_skill_P4F01():
    from collimate.handlers import validate_added_skills
    with pytest.raises(ValueError):
        validate_added_skills({'skills/local/SKILL.md': b'---\nname: document-authoring\n---\nOverride'}, ACCEPTED)


@pytest.mark.parametrize('extra', [
    {'skills/Local/SKILL.md': b'Case-alias'},
    {'skills/other/SKILL.md': b'---\nname: local\n---\nAlias'},
    {'skills/local/SKILL.md': b'---\nname: false\n---\nNot a text name'},
    {'skills/local/data.txt': b'x' * (1024 * 1024)},
    {f'skills/local/data-{i}.txt': b'x' for i in range(128)},
])
def test_added_guidance_refuses_ambiguous_names_and_bounds_P4F01(extra):
    from collimate.handlers import validate_added_skills
    with pytest.raises(ValueError):
        validate_added_skills({'skills/local/SKILL.md': b'Local', **extra}, ACCEPTED)


@pytest.mark.parametrize('body', [b'---\r\nname: document-authoring\r\n---\r\nAdded', b'---\nname: " document-authoring "\n---\nAdded'])
def test_effective_runtime_alias_cannot_shadow_accepted_skill_P4F01(body, monkeypatch):
    monkeypatch.syspath_prepend(str(GENERATORS))
    from collimate.handlers import validate_added_skills
    from _agent_shared.cell_caps import _skill_from
    assert _skill_from('local/SKILL.md', body.decode(), []).name == 'document-authoring'
    with pytest.raises(ValueError, match='collid'):
        validate_added_skills({'skills/local/SKILL.md': body}, ACCEPTED)


@pytest.mark.parametrize('accepted_body', [b'---\r\nname: document-authoring\r\n---\r\nAccepted', b'---\nname: " document-authoring "\n---\nAccepted'])
def test_previous_accepted_effective_names_remain_reserved_P4F01(accepted_body):
    from collimate.handlers import validate_added_skills
    with pytest.raises(ValueError, match='collid'):
        validate_added_skills({'skills/local/SKILL.md': b'---\nname: document-authoring\n---\nAdded'},
                              {'skills/author/SKILL.md': accepted_body})


def test_two_added_effective_aliases_must_be_unique_P4F01():
    from collimate.handlers import validate_added_skills
    with pytest.raises(ValueError, match='unique'):
        validate_added_skills({'skills/local/SKILL.md': b'---\r\nname: same\r\n---\r\nOne',
                              'skills/other/SKILL.md': b'---\nname: " same "\n---\nTwo'}, {})
