# SPDX-License-Identifier: MIT
"""The committed module snapshot, the workspace's ``module-snapshot/``.

It is the shipped renderers' whole upstream graph (esm.sh modules and the
jsdelivr assets their loaders fetch), frozen by
``python -m collimate._host.module_mirror freeze`` and shipped in the app's wheel,
whose mirror serves it. These tests hold it to its sources and its contract:
canonical index, content-addressed files, current contract, roots equal to
what the sources load, a closed graph, one version of every package that must
load once, a size budget, and a license record (from an explicit allowlist)
for every package it redistributes. The renderers' ``deps=`` lists pin each
family; this snapshot freezes what they resolved.

An upstream outage at freeze time is a reported gap only for an edge: an
edge the freeze could not fetch is recorded under ``unavailable`` with its
reason, the graph counts it as known and ``python -m collimate._host.module_mirror
check`` reports it without failing. A root is never a gap: a cold build
public esm.sh refuses comes from a local esm.sh of the pinned release
(``freeze --esm-builder``), and ``builders.json`` records the entries it
built.
"""
from __future__ import annotations

import hashlib
import json

from collimate._host.esm_lexer import scan_imports
from collimate._host.module_mirror import (
    BUILDERS_FILE, CONTRACT, FREEZE_COMMAND, builder_problems, closure_problems, contract_hash,
    instance_problems, is_javascript,
    license_problems, read_snapshot, resolve, shipped_roots, snapshot_index_bytes, unavailable_problems,
)

from tests._workspace import MODULE_SNAPSHOT as SNAPSHOT, WORKSPACE


def test_index_is_valid_and_canonical():
    snapshot = read_snapshot(SNAPSHOT)
    assert (SNAPSHOT / "index.json").read_bytes() == snapshot_index_bytes(snapshot), (
        f"index.json is not canonical; rewrite it with {FREEZE_COMMAND}")
    assert set(path.name for path in SNAPSHOT.iterdir()) == {"index.json", "LICENSES.json", BUILDERS_FILE, "modules"}


def test_locally_built_entries_are_recorded_esm_sh_entries():
    """``builders.json`` names the contract's builder image and only esm.sh
    entries of the snapshot (a local build's bytes differ from the public
    instance's, so the record says which bodies those are)."""
    assert builder_problems(SNAPSHOT, read_snapshot(SNAPSHOT)) == []


def test_module_files_are_content_addressed():
    snapshot = read_snapshot(SNAPSHOT, verify=True)
    for entry in snapshot.entries.values():
        body = (SNAPSHOT / "modules" / entry.hash).read_bytes()
        assert hashlib.sha256(body).hexdigest() == entry.hash


def test_snapshot_was_frozen_under_the_current_contract():
    assert read_snapshot(SNAPSHOT).contract == contract_hash(), (
        f"src/collimate/module_mirror_contract.json changed since the freeze; run {FREEZE_COMMAND}")


def test_roots_are_what_the_shipped_sources_load():
    roots = shipped_roots(WORKSPACE)
    frozen = read_snapshot(SNAPSHOT).roots
    added, removed = sorted(set(roots) - set(frozen)), sorted(set(frozen) - set(roots))
    assert not added and not removed, (
        f"the shipped renderers' upstream URLs changed; run {FREEZE_COMMAND}\n"
        f"  new: {added}\n  gone: {removed}")


def test_graph_is_closed():
    """Every root is frozen, and every edge is frozen or a recorded
    ``unavailable`` gap."""
    problems = closure_problems(read_snapshot(SNAPSHOT))
    assert not problems, f"the frozen graph has holes; rerun {FREEZE_COMMAND}:\n  " + "\n  ".join(problems)


def test_every_unavailable_entry_is_a_reported_edge_of_the_frozen_graph():
    """Each URL the last freeze could not fetch carries its reason and is an
    edge some frozen module imports: no stray entry, and no root (a root the
    sources name is never left out, :func:`closure_problems`). ``check``
    reports the gap without failing."""
    snapshot = read_snapshot(SNAPSHOT)
    edges = {target for entry in snapshot.entries.values() for target in entry.imports + entry.dynamic}
    stray = sorted(url for url in snapshot.unavailable if url not in edges or url in snapshot.roots)
    assert not stray, "unavailable entries no frozen module imports; rerun " + FREEZE_COMMAND + ":\n  " + "\n  ".join(stray)
    blank = sorted(url for url, reason in snapshot.unavailable.items() if not reason.strip())
    assert not blank, "unavailable entries without a reason: " + ", ".join(blank)
    assert unavailable_problems(snapshot) == [
        f"unavailable upstream: {url}: {reason}" for url, reason in sorted(snapshot.unavailable.items())]


def test_shared_packages_resolve_to_one_version():
    """React, ProseMirror, CodeMirror and each plugin family's core load as
    one module instance: the frozen graph holds each at one version
    (``SINGLE_INSTANCE``), whatever its unpinned ranges declare."""
    problems = instance_problems(read_snapshot(SNAPSHOT))
    assert not problems, (
        "the frozen graph loads a shared package twice; pin it in the family's deps= list, or move "
        f"the pins so their ranges agree, then {FREEZE_COMMAND}:\n  " + "\n  ".join(problems))


def test_snapshot_is_within_its_size_budget():
    snapshot = read_snapshot(SNAPSHOT)
    size = sum((SNAPSHOT / "modules" / digest).stat().st_size
               for digest in {entry.hash for entry in snapshot.entries.values()})
    assert size <= CONTRACT["snapshot_max_bytes"], (
        f"the snapshot is {size} bytes, over its {CONTRACT['snapshot_max_bytes']}-byte budget")


def test_every_redistributed_package_has_an_allowed_license():
    problems = license_problems(SNAPSHOT, read_snapshot(SNAPSHOT))
    assert not problems, "LICENSES.json:\n  " + "\n  ".join(problems)
    packages = json.loads((SNAPSHOT / "LICENSES.json").read_bytes())["packages"]
    for package, record in packages.items():
        assert set(record) == {"license", "source"} and record["source"].startswith("https://"), package


def test_index_edges_are_the_served_bodies_imports():
    """Each JavaScript entry's recorded edges are what its served body
    imports, resolved against the URL it is served at."""
    snapshot = read_snapshot(SNAPSHOT)
    bad = []
    for url, entry in snapshot.entries.items():
        if not is_javascript(entry.media_type):
            assert entry.imports == () and entry.dynamic == (), url
            continue
        static, dynamic = set(), set()
        body = (SNAPSHOT / "modules" / entry.hash).read_text(encoding="utf-8").removeprefix("﻿")
        for record in scan_imports(body):
            target = resolve(record.name, url) if record.name else None
            if target:
                (dynamic if record.dynamic else static).add(target)
        if (tuple(sorted(static)), tuple(sorted(dynamic))) != (entry.imports, entry.dynamic):
            bad.append(url)
    assert not bad, "index edges differ from the served bodies: " + ", ".join(bad[:10])
