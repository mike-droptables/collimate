"""Channel isolation: a run resolves generators from its OWN channel, never
the globally-active UI channel.

Regression coverage for the cross-channel-contamination fix. Each test stands
up a SECOND channel (kept non-active), installs a generator only there, and
asserts the run-context resolution helpers scope to the channel they're given
rather than `state.active_channel_id`.
"""
from __future__ import annotations

import os


from conftest import install_test_generator


def _make_channel(name: str):
    """Create a fresh, NON-active channel and return it."""
    from state import state
    from models import Channel
    ch = Channel(name=name)
    state.channels.append(ch)
    return ch


def test_find_generator_dir_scoped_to_channel(client):
    from state import state, channel_db_name
    from run_staging import find_generator_dir

    active = next(c for c in state.channels if c.id == state.active_channel_id)
    active_slug = channel_db_name(active)

    other = _make_channel("Channel Two")
    other_slug = channel_db_name(other)
    install_test_generator(name="iso_gen", channel=other)

    # Resolves against its own channel...
    gen_dir = find_generator_dir("iso_gen", other_slug)
    assert gen_dir and os.path.isdir(gen_dir)
    # ...but is invisible to the active channel (where it doesn't live) — no
    # cross-channel leak. (channel is required; there is no active fallback.)
    assert find_generator_dir("iso_gen", active_slug) is None


def test_capabilities_cells_are_scoped_to_their_channel(client):
    from cell_placement import capabilities_stack_cells
    from state import state, channel_db_name
    from run_staging import generator_cell_id

    other = _make_channel("Channel Two")
    install_test_generator(name="iso_gen", channel=other)

    (holder,) = [c for c in capabilities_stack_cells(other)
                 if any(f.name == "iso_gen/choice.generator" for f in c.files)]
    assert generator_cell_id("iso_gen", channel_db_name(other)) == str(holder.id)

    active = next(c for c in state.channels if c.id == state.active_channel_id)
    assert not any(
        any(f.name.endswith("choice.generator") and "iso_gen" in f.content for f in c.files)
        for c in capabilities_stack_cells(active)
    )
    assert generator_cell_id("iso_gen", channel_db_name(active)) is None


def test_manifest_match_beats_earlier_segment_match(client):
    """A Capabilities cell earlier in stack order holding ``runners/<gid>/``
    (a path segment equal to the generator id) must not shadow the later cell
    whose manifest declares the generator."""
    import uuid as _uuid
    from cell_placement import capabilities_stack
    from models import Cell, VirtualFile
    from state import state, channel_db_name
    from run_staging import find_generator_dir, generator_cell_id

    active = next(c for c in state.channels if c.id == state.active_channel_id)
    install_test_generator(name="gen_x")
    stack = capabilities_stack(active)
    gen_cell = stack.cells[-1]
    decoy = Cell(
        id=_uuid.uuid4(), name="Runners",
        files=[VirtualFile(name="runners/gen_x/preset.runner", content="id: gen_x\n")],
    )
    stack.cells.insert(len(stack.cells) - 1, decoy)
    slug = channel_db_name(active)

    assert generator_cell_id("gen_x", slug) == str(gen_cell.id)
    gen_dir = find_generator_dir("gen_x", slug)
    assert gen_dir is not None
    assert os.path.isfile(os.path.join(gen_dir, "choice.generator"))
    assert os.path.isfile(os.path.join(gen_dir, "gen_x.py"))


def test_channel_creation_validates_display_label_without_deriving_an_address(client):
    assert client.post("/api/channels", params={"name": "   "}).status_code == 400
    response = client.post("/api/channels", params={"name": "!!!"})
    assert response.status_code == 200, response.text
    from uuid import UUID
    identity = response.json()["id"]
    assert str(UUID(identity)) == identity
    assert os.path.exists(f".collimate/channels/{identity}.db")


def test_channel_rename_changes_display_only_even_for_symbol_labels(client):
    from state import state
    identity = str(state.active_channel_id)
    path = f".collimate/channels/{identity}.db"
    inode = os.stat(path).st_ino
    response = client.put(f"/api/channels/{identity}/rename", params={"name": "***"})
    assert response.status_code == 200, response.text
    assert os.stat(path).st_ino == inode
    assert client.put(f"/api/channels/{identity}/rename", params={"name": "   "}).status_code == 400


def test_channel_database_identity_survives_display_name_change__F0349__P6F01(client):
    """A changed label keeps the same UUID database and accepted files."""
    from state import state, channel_db_name
    from database import get_db_connection
    from contextlib import closing
    assert client.post("/api/channels", params={"name": "Mixed Name 7"}).status_code == 200
    ch = next(c for c in state.channels if c.name == "Mixed Name 7")
    identity = str(ch.id)
    assert channel_db_name(ch) == identity
    with closing(get_db_connection(identity)) as conn:
        before = conn.execute('SELECT * FROM working_files ORDER BY cell_id,path').fetchall()
    assert client.put(f'/api/channels/{identity}/rename', params={'name':'Changed Label'}).status_code == 200
    assert channel_db_name(ch) == identity
    with closing(get_db_connection(identity)) as conn:
        assert conn.execute('SELECT * FROM working_files ORDER BY cell_id,path').fetchall() == before
