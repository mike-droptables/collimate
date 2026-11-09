"""A cell version's repositories as stored rows (design §3.2, §5).

``cell_repositories`` keys each repository of a version by its folder
(``repo_path``, ``""`` the cell root) and names its state's manifest;
``repository_parts`` holds the manifest and every part it names, content
addressed (lowercase hex sha256 of the body). Parts are written in the
transaction of the rows naming them and never deleted while the DB lives.
Every writer of a ``cells`` row writes that row's repository rows in the
same transaction: ``versions.insert_version`` carries its parent's forward
(:func:`carry_forward`), a copy names the source's (:func:`copy_rows`).

``repository_state`` owns the state's shape and its reader; this module is
the storage side and the snapshot's summary of a row. It runs no git.
"""
import hashlib
import json
from collections.abc import Callable, Mapping

from collimate_server.repository_state import MAX_REPOSITORIES_PER_CELL, RepositoryState, split_config_key
from collimate_server.reserved_paths import is_repo_path


def _require_path(repo_path: str) -> None:
    if not is_repo_path(repo_path):
        raise ValueError(f"not a repository path: {repo_path!r}")


def _check_state(state: RepositoryState) -> None:
    """Refuse a state whose parts do not hash to their keys or whose
    manifest names a part it does not carry."""
    for part_hash, body in state.parts.items():
        if hashlib.sha256(body).hexdigest() != part_hash:
            raise ValueError(f"repository part {part_hash} does not match its body")
    if state.manifest_hash not in state.parts:
        raise ValueError(f"repository state {state.manifest_hash} lacks its manifest")
    missing = set(state.manifest()["parts"].values()) - state.parts.keys()
    if missing:
        raise ValueError(f"repository state {state.manifest_hash} lacks parts {sorted(missing)}")


def write_rows(conn, version_id: str, states: Mapping[str, RepositoryState | None]) -> None:
    """Apply ``states`` to ``version_id``'s rows in the caller's
    transaction: a state's parts are stored (``INSERT OR IGNORE``) and its
    manifest replaces the row at its path; None deletes the row there."""
    for repo_path, state in states.items():
        _require_path(repo_path)
        if state is None:
            conn.execute("DELETE FROM cell_repositories WHERE version_id=? AND repo_path=?",
                         (version_id, repo_path))
            continue
        _check_state(state)
        conn.executemany("INSERT OR IGNORE INTO repository_parts(hash, body) VALUES (?, ?)",
                         list(state.parts.items()))
        conn.execute("INSERT INTO cell_repositories(version_id, repo_path, state_hash) VALUES (?, ?, ?) "
                     "ON CONFLICT(version_id, repo_path) DO UPDATE SET state_hash=excluded.state_hash",
                     (version_id, repo_path, state.manifest_hash))


def copy_rows(conn, version_id: str, rows: Mapping[str, str]) -> None:
    """Give ``version_id`` the rows ``{repo_path: state_hash}`` of states
    this DB already stores (a copy's rows, a re-keyed merge), in the
    caller's transaction."""
    for repo_path in rows:
        _require_path(repo_path)
    conn.executemany("INSERT INTO cell_repositories(version_id, repo_path, state_hash) VALUES (?, ?, ?)",
                     [(version_id, repo_path, state_hash) for repo_path, state_hash in rows.items()])


def rows_of(conn, version_id: str) -> dict[str, str]:
    """``{repo_path: state_hash}`` of the version's repositories, by path."""
    return dict(conn.execute("SELECT repo_path, state_hash FROM cell_repositories "
                             "WHERE version_id=? ORDER BY repo_path", (version_id,)).fetchall())


def carry_forward(conn, from_version_id: str, to_version_id: str,
                  patch: Mapping[str, RepositoryState | None]) -> None:
    """The new version ``to_version_id`` takes ``from_version_id``'s rows,
    then ``patch`` by ``repo_path``: a state replaces the row, None deletes
    it (:func:`write_rows`)."""
    conn.execute("INSERT INTO cell_repositories(version_id, repo_path, state_hash) "
                 "SELECT ?, repo_path, state_hash FROM cell_repositories WHERE version_id=?",
                 (to_version_id, from_version_id))
    write_rows(conn, to_version_id, patch)


def _body(conn, part_hash: str) -> bytes:
    row = conn.execute("SELECT body FROM repository_parts WHERE hash=?", (part_hash,)).fetchone()
    if row is None:
        raise LookupError(f"repository part {part_hash} is not stored")
    return bytes(row[0])


def load_state(conn, state_hash: str) -> RepositoryState:
    """The stored state whose manifest is ``state_hash``: the manifest and
    every part it names. ValueError when a stored body does not hash to
    its key (a corrupt part)."""
    manifest = _body(conn, state_hash)
    parts = {state_hash: manifest}
    for part_hash in json.loads(manifest)["parts"].values():
        parts[part_hash] = _body(conn, part_hash)
    state = RepositoryState(state_hash, parts)
    _check_state(state)
    return state


def _short(ref: str) -> str:
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref


def branch_config(state: RepositoryState) -> tuple[str, str | None, str | None] | None:
    """The ref HEAD names with its ``branch.<b>.remote`` and the first
    ``branch.<b>.merge`` (git's upstream; the last remote wins, as git
    reads a single value), section and variable matched
    case-insensitively, the subsection exactly; None for a detached HEAD."""
    head = state.manifest()["head"]
    if not head.startswith("ref: "):
        return None
    ref = head[len("ref: "):]
    branch = _short(ref)
    remote = merge = None
    config = state.part("config")
    for key, value in json.loads(config) if config is not None else ():
        section, subsection, variable = split_config_key(key)
        if section != "branch" or subsection != branch or not isinstance(value, str):
            continue
        if variable == "remote":
            remote = value
        elif variable == "merge" and merge is None:
            merge = value
    return ref, remote, merge


def summary(state: RepositoryState) -> dict:
    """``{branch, upstream, detached}`` of a state (:func:`branch_config`).
    A detached HEAD has no branch and no upstream; a local upstream
    (remote ``.``) is the branch name alone."""
    found = branch_config(state)
    if found is None:
        return {"branch": None, "upstream": None, "detached": True}
    ref, remote, merge = found
    upstream = None
    if remote and merge:
        upstream = _short(merge) if remote == "." else f"{remote}/{_short(merge)}"
    return {"branch": _short(ref), "upstream": upstream, "detached": False}


def snapshot_entries(conn, version_id: str, *,
                     counts: Callable[[str, str, Callable[[], RepositoryState]], dict] | None = None,
                     rescued: Mapping[str, Mapping] | None = None,
                     failed: Mapping[str, Mapping] | None = None) -> tuple[list[dict], int]:
    """The state snapshot's ``repositories`` of a version and its
    ``repositories_overflow``: one ``{path, branch, upstream, ahead,
    behind, detached}`` per row, shallowest first then by path, at most
    ``MAX_REPOSITORIES_PER_CELL`` (the rest counted, never dropped from the
    rows). Only the manifest and config part are read.

    ``counts(repo_path, state_hash, load)`` gives a row's ``ahead`` and
    ``behind`` (and an ``error`` when git refused), ``load`` reading the
    whole state on demand; without it both are null. ``rescued`` maps a
    path to its rescue record (``git_store.rescues_of``): that entry gets
    ``error: {reason, rescued: true}``, and a rescued path without a row
    gets an entry of its own. ``failed`` maps a row's path to the error of
    its last materialize (``{reason}``): that entry, unless rescued, gets
    ``error: {reason, rescued: false}``."""
    rows = rows_of(conn, version_id)
    rescued = rescued or {}
    paths = set(rows) | set(rescued)
    ordered = sorted(paths, key=lambda path: (path.count("/") + (path != ""), path))
    entries = []
    for repo_path in ordered[:MAX_REPOSITORIES_PER_CELL]:
        state_hash = rows.get(repo_path)
        entry: dict = {"path": repo_path, "branch": None, "upstream": None, "ahead": None, "behind": None,
                       "detached": False}
        if state_hash is not None:
            manifest = _body(conn, state_hash)
            parts = {state_hash: manifest}
            config_hash = json.loads(manifest)["parts"].get("config")
            if config_hash is not None:
                parts[config_hash] = _body(conn, config_hash)
            entry.update(summary(RepositoryState(state_hash, parts)))
            if counts is not None:
                entry.update(counts(repo_path, state_hash, lambda state_hash=state_hash: load_state(conn, state_hash)))
        record = rescued.get(repo_path)
        if record is not None:
            entry["error"] = {"reason": str(record.get("reason") or "git history not kept"), "rescued": True}
        elif state_hash is not None and repo_path in (failed or {}):
            entry["error"] = {"reason": str((failed or {})[repo_path].get("reason") or "materialize failed"),
                              "rescued": False}
        entries.append(entry)
    return entries, max(0, len(ordered) - MAX_REPOSITORIES_PER_CELL)
