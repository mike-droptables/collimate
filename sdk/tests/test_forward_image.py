"""D23 command images carry the current SDK and uv, with stable staged bytes:
the host's base image (``api/collimate_server/process_manager/base_image``) stages the SDK it
runs and installs it into the image's own Python."""
from pathlib import Path

from tests._workspace import host_base_image


def test_command_base_stages_current_sdk_into_the_hosts_context_D23(tmp_path):
    base = host_base_image()
    staged = base.stage(tmp_path / 'context')
    context = Path(staged.context)
    assert (context / 'sdk/collimate/testing/forward.py').read_bytes() == (
        Path(__file__).parents[1] / 'src/collimate/testing/forward.py').read_bytes()
    assert (context / 'sdk/gen/__init__.py').read_bytes() == (
        Path(__file__).parents[1] / 'src/gen/__init__.py').read_bytes()
    dockerfile = (context / 'Dockerfile').read_text()
    assert dockerfile == (base.SOURCE / 'Dockerfile').read_text()
    assert 'uv python install' in dockerfile
    # The SDK is linked into the image venv beside its staged metadata, never
    # put on an image-wide PYTHONPATH that would shadow a project venv.
    assert not any('PYTHONPATH' in line for line in dockerfile.splitlines()
                   if line.startswith('ENV '))
    assert ('ln -s /opt/collimate/collimate /opt/collimate/gen '
            '/opt/collimate/collimate-*.dist-info "$site/"') in dockerfile
    # uv's managed interpreter reads the image venv through a .pth bridge, and
    # login shells get the venv back in front of PATH from profile.d.
    assert 'collimate-python.pth' in dockerfile
    assert '/etc/profile.d/collimate-python.sh' in dockerfile
    assert len(list(context.glob('sdk/collimate-*.dist-info/METADATA'))) == 1
    # Only the SDK subset: its own requirements, never the server or its dependencies.
    from collimate.shared import SDK_REQUIREMENTS
    assert (context / 'sdk-requirements.txt').read_text().split() == list(SDK_REQUIREMENTS)
    assert not list(context.rglob('collimate_server'))
    # The tag is the staged content's: staging again names the same image.
    assert staged.tag.startswith(base.BASE_IMAGE + ':') and len(staged.tag.partition(':')[2]) == 12
    assert base.stage(tmp_path / 'again').tag == staged.tag


def test_the_staged_base_is_private_and_removed_after_use():
    base = host_base_image()
    with base.staged() as staged:
        context = Path(staged.context)
        assert (context / 'Dockerfile').is_file()
        assert context.stat().st_mode & 0o077 == 0
    assert not context.exists()


def test_an_image_layered_on_the_base_is_tagged_by_the_host(tmp_path):
    """The layered tag folds in the context, the base's digest and the build
    args; an untagged collimate-<name> is the only name a layered image takes."""
    import pytest
    base = host_base_image()
    staged = base.stage(tmp_path / 'context')
    tool = tmp_path / 'tool'
    tool.mkdir()
    (tool / 'Dockerfile').write_text('ARG BASE=collimate-linux-small:v3\nFROM ${BASE}\n')
    args = {'BASE': staged.tag, 'TOOL_VERSION': '1'}
    first = base.layered_tag('collimate-tool', str(tool), staged, args)
    assert first.startswith('collimate-tool:')
    assert base.layered_tag('collimate-tool', str(tool), staged, {**args, 'TOOL_VERSION': '2'}) != first
    other = base.StagedBase(staged.context, '0' * 64, staged.tag)
    assert base.layered_tag('collimate-tool', str(tool), other, args) != first
    for name in ('collimate-tool:latest', 'tool', 'collimate-tool@sha256:' + 'a' * 64):
        with pytest.raises(ValueError, match='untagged'):
            base.layered_tag(name, str(tool), staged, args)
