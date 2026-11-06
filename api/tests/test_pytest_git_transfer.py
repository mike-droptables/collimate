"""A cell's git state through export, import and a new channel (design §7):
an export writes each repository as a real ``.git`` (zip, or a folder
export's staged files through its token) that ``git status`` finds clean
and ``git fsck --full`` accepts; an import adopts a ``.git`` it finds, and
leaves out with git's words what git would not keep; a new channel from a
channel copies the store so its runs see the full history. Every case also
holds without host git: the ``.git`` is left out and said so.

Repositories are made with plain git under ``tmp_path``; their cells come
from real harvests (``test_pytest_git_harvest``'s run harness)."""
from __future__ import annotations

import io
import json
import os
import shutil
import time
import uuid
import zipfile

import pytest

from collimate_server.database import get_db_connection
from git_repos import GIT_OK, commit, git, init
from test_pytest_git_harvest import (Run, _clean as _clean, clone_into, cloned_cell, new_cell, remote, rows, state_at,
                                     status)
from test_pytest_options_identity import CHANNEL, client as client

pytestmark = GIT_OK

PROVENANCE = ".collimate-provenance.json"


# --- the harness ------------------------------------------------------------------------------------


def cell_from(tmp_path, repo, *, folder="proj", name="ws-make", skip=()) -> str:
    """A cell whose run cloned ``repo`` (a work tree, cloned as it is) into
    ``folder``; its id."""
    cid = new_cell()
    run = Run(cid, tmp_path / name)
    git(run.ws, "clone", "-q", "--no-local", repo, folder)
    assert run.finish(skip=tuple(f"{folder}/{path}" for path in skip)).version_id
    return cid


def unzip(body: bytes, target) -> list[zipfile.ZipInfo]:
    """Extract an export zip as a user's unzip does: files with their modes."""
    target = str(target)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        infos = archive.infolist()
        for info in infos:
            out = os.path.join(target, *info.filename.rstrip("/").split("/"))
            if info.is_dir():
                os.makedirs(out, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with archive.open(info) as source, open(out, "wb") as sink:
                shutil.copyfileobj(source, sink)
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                os.chmod(out, mode)
    return infos


def export_zip(client, cid, **params) -> bytes:
    response = client.get(f"/api/channels/{CHANNEL}/export.zip", params={"scope": "cell", "cell_id": cid, **params})
    assert response.status_code == 200, response.text
    return response.content


def export_folder(client, cid, target, **params) -> dict:
    """A folder export as the client writes one: every entry fetched (a cell
    file from the CAS, a staged file through the token), the token released."""
    manifest = client.get(f"/api/channels/{CHANNEL}/export/manifest",
                          params={"scope": "cell", "cell_id": cid, **params}).json()
    for folder in manifest["dirs"]:
        os.makedirs(os.path.join(str(target), *folder.split("/")), exist_ok=True)
    for entry in manifest["entries"]:
        if "body_hash" in entry:
            response = client.get(f"/api/channels/{CHANNEL}/blob/{entry['body_hash']}")
        else:
            response = client.get(f"/api/channels/{CHANNEL}/export/{manifest['token']}/file",
                                  params={"path": entry["export_file"]})
        assert response.status_code == 200, (entry, response.text)
        out = os.path.join(str(target), *entry["path"].split("/"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as fh:
            fh.write(response.content)
    if manifest["token"]:
        assert client.delete(f"/api/channels/{CHANNEL}/export/{manifest['token']}").status_code == 204
    return manifest


def assert_clean(work_tree, commits: int | None = None) -> None:
    """``git status`` clean, ``git fsck --full`` passes, and the log is whole."""
    assert status(work_tree) == ""
    git(work_tree, "fsck", "--full", "--no-dangling")
    if commits is not None:
        assert int(git(work_tree, "rev-list", "--count", "HEAD").strip()) == commits


def no_git(monkeypatch):
    import collimate_server.git_host as git_host
    monkeypatch.setattr(git_host, "capability", lambda: git_host.Capability("missing"))


# --- export -------------------------------------------------------------------------------------------


def test_a_clean_repository_exports_clean_and_whole_by_zip_and_by_folder(client, tmp_path):
    cid, url, _source = cloned_cell(tmp_path)
    run = Run(cid, tmp_path / "ws-commit")
    git(run.path("proj"), "commit", "-q", "--allow-empty", "-m", "local")
    run.finish()
    zipped = tmp_path / "zipped"
    infos = unzip(export_zip(client, cid), zipped)
    assert_clean(zipped / "Repo" / "proj", commits=2)
    names = {info.filename for info in infos}
    # Git's empty folders travel as folders; the packs fill objects/pack.
    for folder in ("refs/heads/", "refs/tags/", "objects/info/"):
        assert f"Repo/proj/.git/{folder}" in names
    assert [name for name in names if name.startswith("Repo/proj/.git/objects/pack/pack-")]
    assert git(zipped / "Repo" / "proj", "config", "--get", "remote.origin.url").strip() == url
    folder = tmp_path / "folder"
    manifest = export_folder(client, cid, folder)
    assert manifest["token"] and manifest["notes"] == []
    assert any(e["path"].startswith("Repo/proj/.git/objects/pack/") and "export_file" in e
               for e in manifest["entries"])
    assert_clean(folder / "Repo" / "proj", commits=2)
    # Released: the staged files are gone.
    staged = next(e for e in manifest["entries"] if "export_file" in e)
    gone = client.get(f"/api/channels/{CHANNEL}/export/{manifest['token']}/file", params={"path": staged["export_file"]})
    assert gone.status_code == 404
    assert not [name for name in os.listdir(".collimate/tmp") if name.startswith("export-")]


def test_a_root_repository_exports_its_exec_bits_links_and_case_collisions_clean(client, tmp_path):
    source = init(tmp_path / "src")
    commit(source, {"run.sh": "#!/bin/sh\necho hi\n", "Read.md": "upper\n", "read.md": "lower\n",
                    "docs/a.md": "a\n"}, "first")
    os.chmod(os.path.join(source, "run.sh"), 0o755)
    os.symlink("docs/a.md", os.path.join(source, "link.md"))
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "mode and link")
    cid = new_cell()
    run = Run(cid, tmp_path / "ws-root")
    git(run.ws, "init", "-q")
    git(run.ws, "fetch", "-q", source, "main:main")
    git(run.ws, "symbolic-ref", "HEAD", "refs/heads/main")
    git(run.ws, "reset", "-q", "--hard")
    assert run.finish().version_id
    assert "" in rows(cid)
    target = tmp_path / "out"
    infos = {info.filename: info for info in unzip(export_zip(client, cid), target)}
    work = target / "Repo"
    # The second spelling is renamed by the export: HEAD's own path is
    # skip-worktree and the name it was written under is excluded.
    renamed = "read (2).md" if "Repo/read (2).md" in infos else "Read (2).md"
    assert status(work) == ""
    git(work, "fsck", "--full", "--no-dangling")
    import collimate_server.repository_state as rs
    exclude = (work / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude[exclude.index(rs.RENAMED_BEGIN) + 1:exclude.index(rs.RENAMED_END)] == [f"/{renamed}"]
    assert git(work, "check-ignore", "-q", renamed) == ""
    assert int(git(work, "rev-list", "--count", "HEAD").strip()) == 2
    assert (infos["Repo/run.sh"].external_attr >> 16) == 0o100755
    assert (infos["Repo/docs/a.md"].external_attr >> 16) == 0o100644
    # The link is left out (no delivery carries one): skip-worktree too.
    flags = {line[2:]: line[0] for line in git(work, "ls-files", "-v").splitlines()}
    assert flags["link.md"] == "S"
    assert sorted(path for path, flag in flags.items() if flag == "S" and path != "link.md") in (["read.md"], ["Read.md"])


def test_an_oversize_file_kept_out_of_the_cell_exports_clean(client, tmp_path, monkeypatch):
    import collimate_server.cas as cas
    source = init(tmp_path / "src")
    commit(source, {"big.bin": "x" * 4096, "small.txt": "s\n"}, "first")
    monkeypatch.setattr(cas, "MAX_BLOB_BYTES", 1024)
    cid = cell_from(tmp_path, source, skip=("big.bin",))
    assert b"big.bin" in (state_at(cid, "proj").part("kept_out") or b"")
    target = tmp_path / "out"
    exported = export_zip(client, cid)
    unzip(exported, target)
    assert not (target / "Repo" / "proj" / "big.bin").exists()
    assert_clean(target / "Repo" / "proj", commits=1)
    # Imported back, the file the export kept out is kept out again: no
    # deletion the user never made.
    expansion = expand(client, exported).json()
    (held,) = expansion["repositories"]
    done = import_expanded(client, expansion, "Repo", [{"repo_path": "proj", "token": held["token"]}])
    (copy,) = done["cell_ids"]
    assert state_at(copy, "proj").part("kept_out") == state_at(cid, "proj").part("kept_out")
    run = Run(copy, tmp_path / "ws-reimported")
    assert status(run.path("proj")) == ""


def test_a_shallow_repository_exports_clean_with_its_boundary(client, tmp_path):
    url, source = remote(tmp_path)
    commit(source, {"two.txt": "2\n"}, "second")
    git(source, "push", "-q", url.removeprefix("file://"), "main")
    cid = new_cell()
    run = Run(cid, tmp_path / "ws-shallow")
    git(run.ws, "clone", "-q", "--depth", "1", url, "proj")
    assert run.finish().version_id
    target = tmp_path / "out"
    unzip(export_zip(client, cid), target)
    work = target / "Repo" / "proj"
    assert (work / ".git" / "shallow").is_file()
    assert_clean(work, commits=1)


def test_omitting_local_history_drops_reflogs_and_stash_and_says_so(client, tmp_path):
    cid, _url, _source = cloned_cell(tmp_path)
    run = Run(cid, tmp_path / "ws-stash")
    with open(run.path("proj", "README.md"), "a") as fh:
        fh.write("edit\n")
    git(run.path("proj"), "stash", "-q")
    run.finish()
    assert "refs/stash" in (state_at(cid, "proj").part("refs") or b"").decode()
    kept = tmp_path / "kept"
    unzip(export_zip(client, cid), kept)
    assert git(kept / "Repo" / "proj", "stash", "list").strip()
    assert (kept / "Repo" / "proj" / ".git" / "logs" / "HEAD").is_file()
    omitted = tmp_path / "omitted"
    infos = unzip(export_zip(client, cid, omit_git_history="true"), omitted)
    work = omitted / "Repo" / "proj"
    assert git(work, "stash", "list").strip() == ""
    assert not [info for info in infos if "/.git/logs/" in info.filename and not info.is_dir()]
    assert_clean(work, commits=1)
    notes = json.loads((omitted / "Repo" / PROVENANCE).read_text())["notes"]
    assert notes == ["Repo/proj: the stash was not exported (local git history omitted)"]
    manifest = export_folder(client, cid, tmp_path / "folder", omit_git_history="true")
    assert manifest["notes"] == notes


def test_what_building_a_git_warns_of_is_a_note(client, tmp_path, monkeypatch):
    import collimate_server.repository_state as rs
    cid, _url, _source = cloned_cell(tmp_path)
    run = Run(cid, tmp_path / "ws-merge")
    proj = run.path("proj")
    git(proj, "checkout", "-q", "-b", "side")
    commit(proj, {"side.txt": "s\n"}, "side")
    git(proj, "checkout", "-q", "main")
    commit(proj, {"main.txt": "m\n"}, "main")
    git(proj, "merge", "-q", "--no-commit", "--no-ff", "side")
    assert run.finish().version_id
    # The stored merge is refused on the way out (the recheck before any
    # write), so the export leaves it behind and says why.
    monkeypatch.setattr(rs, "progress_problem", lambda progress: "the merge state is refused")
    target = tmp_path / "out"
    unzip(export_zip(client, cid), target)
    notes = json.loads((target / "Repo" / PROVENANCE).read_text())["notes"]
    assert notes == ["Repo/proj/.git: the merge state is refused"]
    assert not (target / "Repo" / "proj" / ".git" / "MERGE_HEAD").exists()


def test_a_cell_named_like_git_never_exports_as_a_git_folder(client, tmp_path):
    cid, _url, _source = cloned_cell(tmp_path)
    from collimate_server.state import require_channel, state_lock
    with state_lock:
        cell = next(c for s in require_channel(CHANNEL).main_stacks for c in s.cells if str(c.id) == cid)
        cell.name = ".git."
    names = {info.filename.split("/")[0] for info in unzip(export_zip(client, cid), tmp_path / "out")}
    assert names == {"_.git"}


def test_without_git_an_export_leaves_the_git_out_and_says_so(client, tmp_path, monkeypatch):
    cid, _url, _source = cloned_cell(tmp_path)
    no_git(monkeypatch)
    target = tmp_path / "out"
    infos = unzip(export_zip(client, cid), target)
    assert not [info for info in infos if "/.git/" in info.filename]
    assert (target / "Repo" / "proj" / "README.md").is_file()
    notes = json.loads((target / "Repo" / PROVENANCE).read_text())["notes"]
    assert notes == ["Repo/proj/.git: not exported: git on this machine is missing; install git 2.31 or later"]
    manifest = export_folder(client, cid, tmp_path / "folder")
    assert manifest["notes"] == notes
    assert not [e for e in manifest["entries"] if "/.git/" in e["path"]]
    # The one staged file is the provenance file that says so.
    assert [e["path"] for e in manifest["entries"] if "export_file" in e] == [f"Repo/{PROVENANCE}"]


# --- import -------------------------------------------------------------------------------------------


def zip_tree(root, prefix: str = "", *, extra: dict | None = None) -> bytes:
    """A zip of everything under ``root`` (its ``.git`` included, empty
    folders as entries) at ``prefix``; ``extra`` adds members by name."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for here, folders, names in os.walk(str(root)):
            rel = os.path.relpath(here, str(root)).replace(os.sep, "/")
            base = "/".join(part for part in (prefix, "" if rel == "." else rel) if part)
            if base and not names and not folders:
                archive.writestr(base + "/", b"")
            for name in names:
                path = os.path.join(here, name)
                if os.path.islink(path):
                    continue
                archive.write(path, f"{base}/{name}" if base else name)
        for name, body in (extra or {}).items():
            archive.writestr(name, body)
    return buffer.getvalue()


def expand(client, body: bytes):
    return client.post(f"/api/channels/{CHANNEL}/import/zip", content=body)


def import_expanded(client, expansion: dict, folder: str, repositories: list[dict], name="Imported") -> dict:
    """One new cell of the expansion's files under ``folder``, adopting
    ``repositories``; the import's answer."""
    prefix = f"{folder}/" if folder else ""
    files = [{"path": e["path"][len(prefix):], "blob_hash": e["blob_hash"]} for e in expansion["entries"]
             if e["path"].startswith(prefix)]
    response = client.post(f"/api/channels/{CHANNEL}/import", json={"stacks": [{"name": "In", "cells": [
        {"name": name, "files": files, "dirs": [], "repositories": repositories}]}]})
    assert response.status_code == 200, response.text
    return response.json()


def project(tmp_path, name="app", files=None) -> str:
    repo = init(tmp_path / name)
    commit(repo, files or {"README.md": "hello\n", "src/app.py": "print(1)\n"}, "first")
    commit(repo, {"src/app.py": "print(2)\n"}, "second")
    return repo


def test_an_imported_zip_with_a_git_is_adopted_and_a_run_sees_it_clean(client, tmp_path):
    repo = project(tmp_path)
    git(repo, "remote", "add", "origin", "https://user:secret@example.com/app.git")
    expansion = expand(client, zip_tree(repo, "App")).json()
    assert not [e for e in expansion["entries"] if "/.git/" in e["path"]]
    (held,) = expansion["repositories"]
    assert held["dir"] == "App"
    assert os.path.isdir(os.path.join(".collimate", "imports", held["token"], ".git"))
    done = import_expanded(client, expansion, "App", [{"repo_path": "", "token": held["token"]}])
    assert done["skipped"] == []
    (cid,) = done["cell_ids"]
    assert set(rows(cid)) == {""}
    assert not os.path.exists(os.path.join(".collimate", "imports", held["token"]))
    config = state_at(cid).part("config").decode()
    assert "secret" not in config and "example.com/app.git" in config
    run = Run(cid, tmp_path / "ws-adopted")
    assert status(run.ws) == ""
    assert int(git(run.ws, "rev-list", "--count", "HEAD").strip()) == 2


def test_an_export_imports_back_as_the_same_repository(client, tmp_path):
    cid, _url, _source = cloned_cell(tmp_path)
    state = state_at(cid, "proj")
    expansion = expand(client, export_zip(client, cid)).json()
    (held,) = expansion["repositories"]
    assert held["dir"] == "Repo/proj"
    done = import_expanded(client, expansion, "Repo", [{"repo_path": "proj", "token": held["token"]}])
    (copy,) = done["cell_ids"]
    assert state_at(copy, "proj").manifest_hash == state.manifest_hash


def test_an_import_refuses_shared_objects_and_a_collrun_head_and_skips_gitfiles(client, tmp_path):
    repo = project(tmp_path)
    shared = str(tmp_path / "shared")
    git(tmp_path, "clone", "-q", "--shared", repo, shared)
    band = init(tmp_path / "band")
    commit(band, {".collrun/x": "x\n", "f": "f\n"}, "band")
    shutil.rmtree(os.path.join(band, ".collrun"))
    expansion_s = expand(client, zip_tree(shared, "S", extra={"S/sub/.git": "gitdir: ../.git/modules/sub\n",
                                                               "S/sub/file.txt": "sub\n"})).json()
    assert {"path": "S/sub/.git", "reason": "submodule/worktree gitfile (not supported)"} in expansion_s["skipped"]
    assert "S/sub/file.txt" in [e["path"] for e in expansion_s["entries"]]
    (held,) = expansion_s["repositories"]
    done = import_expanded(client, expansion_s, "S", [{"repo_path": "", "token": held["token"]}])
    import collimate_server.repository_state as rs
    assert done["skipped"] == [{"path": ".git", "reason": rs.BORROWS}]
    (cid,) = done["cell_ids"]
    assert rows(cid) == {}
    from collimate_server.persistence import snapshot_files
    conn = get_db_connection(CHANNEL)
    try:
        assert "README.md" in {f["path"] for f in snapshot_files(conn, cid)}
    finally:
        conn.close()
    expansion_b = expand(client, zip_tree(band, "B")).json()
    (held,) = expansion_b["repositories"]
    done = import_expanded(client, expansion_b, "B", [{"repo_path": "", "token": held["token"]}], name="Band")
    assert done["skipped"] == [{"path": ".git", "reason": "HEAD holds .collrun/ at its top"}]


def test_an_import_says_in_gits_words_what_fsck_refused(client, tmp_path):
    from test_pytest_git_harvest import commit_object, tree_with
    ws = init(tmp_path / "bad")
    blob = git(ws, "hash-object", "-w", "--stdin", input=b"x").strip()
    tip = commit_object(ws, tree_with(ws, b"100644 .git\0" + bytes.fromhex(blob)))
    git(ws, "update-ref", "refs/heads/main", tip)
    expansion = expand(client, zip_tree(ws, "Bad", extra={"Bad/notes.md": "n\n"})).json()
    (held,) = expansion["repositories"]
    done = import_expanded(client, expansion, "Bad", [{"repo_path": "lib", "token": held["token"]}])
    (skipped,) = done["skipped"]
    assert skipped["path"] == "lib/.git"
    assert skipped["reason"].startswith("objects refused; git: index-pack exited")
    assert "hasDotgit" in skipped["reason"]
    # Not adopted, so the upload stays for the sweep.
    assert os.path.isdir(os.path.join(".collimate", "imports", held["token"]))


def test_a_git_under_a_run_folder_is_a_400_and_modules_and_worktrees_are_left_out(client, tmp_path):
    for bad in ("_runs/x/.git/HEAD", ".collrun/.git/HEAD", "a/_runs/.git/config"):
        assert expand(client, zip_tree(tmp_path / "none", extra={bad: "ref: refs/heads/main\n"})).status_code == 400
    for clash in ({".git/a": "x", ".git/a/b": "y"}, {".git/a/b": "y", ".git/a": "x"}):
        assert expand(client, zip_tree(tmp_path / "none", extra=clash)).status_code == 400
    repo = project(tmp_path)
    expansion = expand(client, zip_tree(repo, "", extra={".git/modules/sub/HEAD": "ref: refs/heads/main\n",
                                                          ".git/worktrees/w/HEAD": "x\n"})).json()
    (held,) = expansion["repositories"]
    assert held["dir"] == ""
    gitdir = os.path.join(".collimate", "imports", held["token"], ".git")
    assert not os.path.exists(os.path.join(gitdir, "modules")) and not os.path.exists(os.path.join(gitdir, "worktrees"))


def multipart(body: bytes, name="file") -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    form = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"note\"\r\n\r\nignored\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"repository.zip\"\r\n"
            f"Content-Type: application/zip\r\n\r\n").encode() + body + f"\r\n--{boundary}--\r\n".encode()
    return form, f"multipart/form-data; boundary={boundary}"


def test_one_uploaded_git_is_set_aside_by_its_own_route(client, tmp_path):
    import collimate_server.cas as cas
    repo = project(tmp_path)
    stale = os.path.join(".collimate", "imports", uuid.uuid4().hex)
    os.makedirs(stale)
    old = os.path.getmtime(stale) - cas.STALE_UPLOAD_SECONDS - 60
    os.utime(stale, (old, old))
    only_git = zip_tree(os.path.join(repo, ".git"), "lib/.git")
    form, kind = multipart(only_git)
    response = client.post(f"/api/channels/{CHANNEL}/import/repository", content=form, headers={"Content-Type": kind})
    assert response.status_code == 200, response.text
    held = response.json()
    assert held["dir"] == "lib" and os.path.isdir(os.path.join(".collimate", "imports", held["token"], ".git"))
    assert not os.path.exists(stale)
    done = import_expanded(client, {"entries": []}, "", [{"repo_path": "lib", "token": held["token"]}])
    assert done["skipped"] == [] and set(rows(done["cell_ids"][0])) == {"lib"}
    for refused in (zip_tree(repo, "lib"), zip_tree(tmp_path / "none", extra={"lib/.git": "gitdir: x\n"})):
        form, kind = multipart(refused)
        assert client.post(f"/api/channels/{CHANNEL}/import/repository", content=form, headers={"Content-Type": kind}).status_code == 400
    form, kind = multipart(only_git, name="other")
    assert client.post(f"/api/channels/{CHANNEL}/import/repository", content=form, headers={"Content-Type": kind}).status_code == 400
    assert client.post(f"/api/channels/{CHANNEL}/import/repository", content=only_git,
                       headers={"Content-Type": "application/zip"}).status_code == 400


def test_an_import_token_that_is_gone_or_not_one_is_refused(client, tmp_path):
    for token, code in (("t-app", 400), (uuid.uuid4().hex, 409)):
        response = client.post(f"/api/channels/{CHANNEL}/import", json={"stacks": [{"name": "In", "cells": [
            {"name": "C", "files": [], "repositories": [{"repo_path": "", "token": token}]}]}]})
        assert response.status_code == code, response.text


def test_without_git_an_import_takes_the_files_and_not_the_git(client, tmp_path, monkeypatch):
    repo = project(tmp_path)
    expansion = expand(client, zip_tree(repo, "App")).json()
    (held,) = expansion["repositories"]
    no_git(monkeypatch)
    done = import_expanded(client, expansion, "App", [{"repo_path": "", "token": held["token"]}])
    assert done["skipped"] == [{"path": ".git", "reason": "git repository not adopted: git not available"}]
    assert rows(done["cell_ids"][0]) == {}


# --- a new channel from a channel --------------------------------------------------------------------


def new_channel(client, config: dict) -> dict:
    response = client.post("/api/channels", params={"name": f"Copy {uuid.uuid4().hex[:6]}"}, json={"config": config})
    assert response.status_code == 200, response.text
    return response.json()


def rows_in(channel: str, cell_id: str) -> dict:
    from collimate_server.repository_rows import rows_of
    from collimate_server.versions import find_tip
    conn = get_db_connection(channel)
    try:
        return rows_of(conn, find_tip(conn, cell_id))
    finally:
        conn.close()


def materialized_in(channel: str, cell_id: str, repo_path: str, work_tree) -> str:
    """The cell's repository written as a run's ``.git`` from ``channel``'s
    store over the cell's files, as a run in that channel gets it."""
    import collimate_server.git_materialize as git_materialize
    import collimate_server.git_store as git_store
    import collimate_server.run_staging as run_staging
    from collimate_server.repository_rows import load_state
    hydrated = run_staging.hydrate_workspace(channel, cell_id, str(work_tree))
    conn = get_db_connection(channel)
    try:
        state = load_state(conn, rows_in(channel, cell_id)[repo_path])
    finally:
        conn.close()
    folder = os.path.join(str(work_tree), *repo_path.split("/")) if repo_path else str(work_tree)
    git_materialize.write_gitdir(folder, state, git_store.objects_path(channel), repo_path=repo_path,
                                 hydrated=hydrated)
    return folder


def head_of(state) -> str:
    import collimate_server.repository_state as rs
    return rs.resolve_ref(rs.parse_refs(state.part("refs") or b""), state.manifest()["head"])


def test_a_new_channel_from_a_channel_copies_the_store_and_its_runs_see_the_whole_history(client, tmp_path):
    import collimate_server.git_store as git_store
    url, source = remote(tmp_path)
    second = commit(source, {"two.txt": "2\n"}, "second")
    git(source, "push", "-q", url.removeprefix("file://"), "main")
    full = new_cell()
    run = Run(full, tmp_path / "ws-full")
    clone_into(run, url)
    git(run.path("proj"), "commit", "-q", "--allow-empty", "-m", "unpushed")
    assert run.finish().version_id
    # A shallow clone whose boundary, the second commit, has a parent the
    # full one holds: grafting it would cut the full repository's history.
    shallow = new_cell()
    run = Run(shallow, tmp_path / "ws-shallow")
    git(run.ws, "clone", "-q", "--depth", "1", url, "proj")
    assert run.finish().version_id
    assert state_at(shallow, "proj").part("shallow") == f"{second}\n".encode()
    head = head_of(state_at(full, "proj"))
    count = int(git(tmp_path, "--git-dir", git_store.store_path(CHANNEL), "rev-list", "--count", head).strip())
    assert count == 3
    made = new_channel(client, {"main_cells": {"channel_id": CHANNEL}})
    copy_channel = str(made["id"])
    copies = {str(cell["id"]) for stack in made["main_stacks"] for cell in stack["cells"]}
    held = {cid: rows_in(copy_channel, cid) for cid in copies}
    assert sorted(set(r) for r in held.values()) == [{"proj"}, {"proj"}]
    store = git_store.store_path(copy_channel)
    assert git(tmp_path, "--git-dir", store, "for-each-ref") == ""
    assert not os.path.exists(os.path.join(store, "shallow"))
    assert int(git(tmp_path, "--git-dir", store, "rev-list", "--count", head).strip()) == count
    full_copy = next(cid for cid, r in held.items() if r["proj"] == state_at(full, "proj").manifest_hash)
    work = materialized_in(copy_channel, full_copy, "proj", tmp_path / "ws-copy")
    assert status(work) == ""
    assert int(git(work, "rev-list", "--count", "HEAD").strip()) == 3
    shallow_copy = next(iter(copies - {full_copy}))
    work = materialized_in(copy_channel, shallow_copy, "proj", tmp_path / "ws-copy-shallow")
    assert status(work) == "" and int(git(work, "rev-list", "--count", "HEAD").strip()) == 1


def test_a_tagged_removed_cell_is_shelved_with_its_rows(client, tmp_path):
    import collimate_server.git_store as git_store
    cid, _url, _source = cloned_cell(tmp_path)
    assert client.patch(f"/api/channels/{CHANNEL}/cells/{cid}/meta", json={"tags": ["keep"]}).status_code == 200
    assert client.post(f"/api/channels/{CHANNEL}/cells/{cid}/remove").status_code == 200
    made = new_channel(client, {"history_cells": {"channel_id": CHANNEL, "tag": "keep"}})
    copy_channel = str(made["id"])
    import collimate_server.removed_cells as removed_cells
    conn = get_db_connection(copy_channel)
    try:
        (record,) = removed_cells.list_removed(conn)
    finally:
        conn.close()
    assert set(rows_in(copy_channel, record.cell_id)) == {"proj"}
    head = head_of(state_at(cid, "proj"))
    git(tmp_path, "--git-dir", git_store.store_path(copy_channel), "cat-file", "-e", head)


def test_without_git_a_new_channel_copies_the_files_and_no_rows(client, tmp_path, monkeypatch, caplog):
    cid, _url, _source = cloned_cell(tmp_path)
    no_git(monkeypatch)
    made = new_channel(client, {"main_cells": {"channel_id": CHANNEL}})
    ((copy, name),) = [(str(cell["id"]), cell["name"]) for stack in made["main_stacks"] for cell in stack["cells"]]
    assert rows_in(str(made["id"]), copy) == {}
    reason = "repository history not copied: git is not available on this host"
    assert made["skipped"] == [{"path": f"{name}/proj/.git", "reason": reason}]
    assert reason in caplog.text


def corrupt_part(cid: str, repo_path: str) -> None:
    """Overwrite one stored part of the cell's row at ``repo_path``."""
    state = state_at(cid, repo_path)
    part = json.loads(state.parts[state.manifest_hash])["parts"]["refs"]
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute("UPDATE repository_parts SET body=? WHERE hash=?", (b"corrupt", part))
        conn.commit()
    finally:
        conn.close()


def test_a_row_that_cannot_be_read_is_left_out_alone(client, tmp_path):
    cid, _url, _source = cloned_cell(tmp_path)
    corrupt_part(cid, "proj")
    target = tmp_path / "out"
    infos = unzip(export_zip(client, cid), target)
    assert not [info for info in infos if "/.git/" in info.filename]
    assert (target / "Repo" / "proj" / "README.md").is_file()
    (note,) = json.loads((target / "Repo" / PROVENANCE).read_text())["notes"]
    assert note.startswith("Repo/proj/.git: not exported: its stored git state cannot be read")
    made = new_channel(client, {"main_cells": {"channel_id": CHANNEL}})
    ((copy, name),) = [(str(cell["id"]), cell["name"]) for stack in made["main_stacks"] for cell in stack["cells"]]
    assert rows_in(str(made["id"]), copy) == {}
    (skipped,) = made["skipped"]
    assert skipped["path"] == f"{name}/proj/.git"
    assert skipped["reason"].startswith("repository history not copied: its stored git state cannot be read")


def test_a_failed_store_copy_unwinds_the_new_channel(client, tmp_path, monkeypatch):
    import collimate_server.channel_paths as channel_paths
    import collimate_server.git_store as git_store
    from collimate_server.state import state
    cloned_cell(tmp_path)

    def refused(source, new, states):
        git_store.ensure(new)
        raise git_store.StoreFailed("repository history not copied: git pack-objects exited 128: fatal: no")

    monkeypatch.setattr(git_store, "copy_between_stores", refused)
    location = channel_paths.default_location().path
    before = set(os.listdir(location))
    names = [c.name for c in state.channels]
    response = client.post("/api/channels", params={"name": "Broken"},
                           json={"config": {"main_cells": {"channel_id": CHANNEL}}})
    assert response.status_code == 409 and "pack-objects exited 128" in response.text
    assert set(os.listdir(location)) <= before
    assert [c.name for c in state.channels] == names


def test_without_git_the_snapshot_keeps_the_branch_with_no_counts(client, tmp_path, monkeypatch):
    import collimate_server.git_store as git_store
    from collimate_server.repository_rows import snapshot_entries
    cid, _url, _source = cloned_cell(tmp_path)
    no_git(monkeypatch)
    git_store.forget_counts()
    conn = get_db_connection(CHANNEL)
    try:
        from collimate_server.versions import find_tip
        tip = find_tip(conn, cid)
        (entry,), overflow = snapshot_entries(
            conn, tip, counts=lambda path, state_hash, load: git_store.ahead_behind(CHANNEL, path, state_hash, load))
    finally:
        conn.close()
    assert overflow == 0
    assert entry["branch"] == "main" and entry["detached"] is False and entry["upstream"]
    assert entry["ahead"] is None and entry["behind"] is None


def test_the_form_reader_takes_the_file_part_whatever_the_chunking(client, tmp_path):
    from collimate_server.transfer_import import receive_form_file
    body = b"PK\x03\x04" + bytes(range(256)) * 8 + b"\r\n--not-the-boundary\r\n"
    form, kind = multipart(body)
    for size in (1, 7, 64, len(form)):
        path = receive_form_file([form[i:i + size] for i in range(0, len(form), size)], kind)
        try:
            with open(path, "rb") as fh:
                assert fh.read() == body
        finally:
            os.unlink(path)


def test_a_folder_exports_staged_files_go_after_their_hour(client, tmp_path, monkeypatch):
    import collimate_server.transfer_export as transfer_export
    monkeypatch.setattr(transfer_export, "STAGING_SECONDS", 0.2)
    token, staging = transfer_export.make_staging()
    with open(os.path.join(staging, "f"), "wb") as fh:
        fh.write(b"x")
    transfer_export.hold_staging(CHANNEL, token, staging)
    assert transfer_export.staged_file(CHANNEL, token, "f") == os.path.join(staging, "f")
    for refused in ("../f", "", "f/..", "missing"):
        with pytest.raises(LookupError):
            transfer_export.staged_file(CHANNEL, token, refused)
    with pytest.raises(LookupError):
        transfer_export.staged_file(str(uuid.uuid4()), token, "f")
    deadline = time.monotonic() + 10
    while os.path.exists(staging) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not os.path.exists(staging)
    with pytest.raises(LookupError):
        transfer_export.staged_file(CHANNEL, token, "f")
