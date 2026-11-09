"""A cell's git state as data (``api/collimate_server/repository_state.py``): discovery, the
reader, the format gate, the allow-list and sanitizer, the operation index,
expiry, caps, the canonical form and its hash, and the roots parser, over
real repositories made with the host git under ``tmp_path``."""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

import collimate_server.repository_state as rs
from git_repos import GIT_OK, commit, git, git_env, init

pytestmark = GIT_OK

DAY = 86400
NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    """The data dir (``.collimate/tmp`` for host git's homes and copies)."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.chdir(data)
    import collimate_server.git_host as git_host
    git_host.reset_capability()


def lookup(gitdir) -> rs.GitdirObjects:
    """Objects over a bare gitdir whose alternates name ``gitdir``'s objects,
    as a harvest's quarantine names the run's and the store's."""
    target = os.path.join(os.path.dirname(str(gitdir)), "lookup.git")
    if not os.path.isdir(target):
        os.makedirs(target)
        git(target, "init", "-q", "--bare")
        with open(os.path.join(target, "objects", "info", "alternates"), "w") as fh:
            fh.write(os.path.join(str(gitdir), "objects") + "\n")
    shallow = os.path.join(str(gitdir), "shallow")
    return rs.GitdirObjects(target, shallow_file=shallow if os.path.exists(shallow) else None)


def read(repo, repo_path="", **kw):
    """Read ``<repo>/.git`` and normalize it against its objects."""
    gitdir = os.path.join(str(repo), ".git")
    got = rs.read_gitdir(gitdir, repo_path=repo_path, alternates=kw.pop("alternates", None))
    return rs.normalize(got, lookup(gitdir), **kw)


def state_of(repo, **kw) -> rs.RepositoryState:
    done = read(repo, **kw)
    assert done.refusal is None, done.refusal
    assert done.state is not None
    return done.state


def write_gitdir(state: rs.RepositoryState, target: str, objects: str, repo_path: str = "") -> None:
    """What materialize writes, enough to read back: HEAD, packed and
    symbolic refs, reflogs, progress, info (with the host's exclude lines),
    shallow, config pairs added with git, the index replayed over
    ``read-tree HEAD``; objects through alternates."""
    parts = state.named_parts()
    os.makedirs(os.path.join(target, "objects", "info"))
    os.makedirs(os.path.join(target, "objects", "pack"))
    os.makedirs(os.path.join(target, "refs", "heads"))
    os.makedirs(os.path.join(target, "refs", "tags"))

    def put(name, body):
        path = os.path.join(target, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(body)

    put("objects/info/alternates", f"{objects}\n".encode())
    put("HEAD", f"{state.manifest()['head']}\n".encode())
    packed = []
    for name, value in rs.parse_refs(parts.get("refs", b"")).items():
        if value.startswith("ref: "):
            put(name, f"{value}\n".encode())
        else:
            packed.append(f"{value} {name}\n")
    put("packed-refs", "".join(sorted(packed, key=lambda line: line.split(" ")[1])).encode())
    host = rs.HOST_EXCLUDES + (rs.HOST_EXCLUDES_ROOT if not repo_path else ())
    put("info/exclude", parts.get("exclude", b"") + "".join(f"{line}\n" for line in host).encode())
    for name, body in parts.items():
        if name.startswith("reflogs/"):
            put("logs/" + name[len("reflogs/"):], body)
        elif name.startswith("progress/"):
            put(name[len("progress/"):], body)
        elif name in ("attributes", "sparse-checkout"):
            put(f"info/{name}", body)
        elif name == "shallow":
            put("shallow", body)
    put("config", b"[core]\n\trepositoryformatversion = 0\n\tbare = false\n")
    env = git_env(target)
    for key, value in json.loads(parts.get("config", b"[]")):
        subprocess.run(["git", "config", "--file", os.path.join(target, "config"), "--add", key, value],
                       check=True, env=env)
    head = state.manifest()["head"]
    tree = ["--empty"] if not rs.resolve_ref(rs.parse_refs(parts.get("refs", b"")), head) else ["HEAD"]
    subprocess.run(["git", f"--git-dir={target}", "read-tree", *tree], check=True, env=env)
    if "index" in parts:
        subprocess.run(["git", f"--git-dir={target}", "update-index", "-z", "--index-info"],
                       input=parts["index"], check=True, env=env)


def round_trip(state, tmp_path, objects, repo_path="", **kw) -> rs.Normalized:
    target = str(tmp_path / "materialized" / ".git")
    write_gitdir(state, target, objects, repo_path)
    got = rs.read_gitdir(target, repo_path=repo_path, alternates=objects)
    return rs.normalize(got, lookup(target), **kw)


def conflicted(tmp_path):
    """A repository stopped in a merge: ``f`` both modified (UU), ``added``
    added and ``gone`` deleted by the other side, ``clean`` modified by it."""
    repo = init(tmp_path / "ws")
    commit(repo, {"f": "base\n", "gone": "g\n", "clean": "c\n"}, "base")
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, {"f": "side\n", "added": "a\n", "gone": None, "clean": "c2\n"}, "side")
    git(repo, "checkout", "-q", "main")
    commit(repo, {"f": "main\n"}, "main")
    git(repo, "merge", "side", check=False)
    assert os.path.exists(os.path.join(repo, ".git", "MERGE_HEAD"))
    return repo


# --- round trip and hash --------------------------------------------------------------------------


def test_the_same_repository_reads_to_the_same_hash(tmp_path):
    repo = conflicted(tmp_path)
    git(repo, "remote", "add", "origin", "https://example.com/r.git")
    first = state_of(repo, now=NOW)
    again = state_of(repo, now=NOW)
    assert first.manifest_hash == again.manifest_hash
    assert first.parts == again.parts
    manifest = first.parts[first.manifest_hash]
    assert manifest == rs.canonical_json(json.loads(manifest))
    assert first.manifest_hash == rs.part_hash(manifest)
    assert all(rs.part_hash(body) == key for key, body in first.parts.items())


def test_a_materialized_state_reads_back_to_its_own_hash(tmp_path):
    """No-op normalize: the normalized form, written as materialize writes
    it and read again, is the same state (sanitized, expired, filtered and
    canonical already)."""
    repo = conflicted(tmp_path)
    git(repo, "remote", "add", "origin", "https://u:tok@example.com/r.git")
    git(repo, "config", "branch.main.remote", "origin")
    git(repo, "config", "include.path", "/etc/elsewhere")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git(repo, "update-ref", "refs/heads/quiet", "HEAD")                      # a tab-less reflog line
    (tmp_path / "ws" / ".git" / "info" / "attributes").write_text("*.bin binary\n")
    with open(tmp_path / "ws" / ".git" / "info" / "exclude", "a") as fh:
        fh.write("/.collrun/\nmine\n.collkeep\n")
    objects = os.path.join(repo, ".git", "objects")
    first = read(repo, now=NOW, kept_out=["big.bin"], secrets=["tok"])
    assert first.state is not None
    back = round_trip(first.state, tmp_path, objects, now=NOW, kept_out=["big.bin"], secrets=["tok"])
    assert back.refusal is None and back.state is not None
    assert back.state.named_parts() == first.state.named_parts()
    assert back.state.manifest_hash == first.state.manifest_hash
    assert back.dropped == () and back.warnings == ()
    exclude = first.state.part("exclude")
    assert exclude is not None and exclude.endswith(b"mine\n") and b"collrun" not in exclude


def test_a_nested_repository_keeps_the_root_band_lines(tmp_path):
    repo = init(tmp_path / "ws" / "sub")
    commit(repo, {"a": "a\n"})
    (tmp_path / "ws" / "sub" / ".git" / "info").mkdir(exist_ok=True)
    (tmp_path / "ws" / "sub" / ".git" / "info" / "exclude").write_text("/_runs/\n.collkeep\n")
    assert state_of(repo, repo_path="sub").part("exclude") == b"/_runs/\n"
    assert state_of(repo, repo_path="").part("exclude") is None


def test_an_exports_renamed_block_is_stripped_whole_on_read():
    """An export's block of the names it wrote files under is a host line
    set of its own: a re-import stores the user's lines only, a last line
    without LF included."""
    body = "".join(f"{line}\n" for line in (
        ".collkeep", rs.RENAMED_BEGIN, "/read (2).md", "/a\\[1].txt", rs.RENAMED_END)).encode() + b"mine\n*.log"
    assert rs.strip_host_excludes(body, "sub") == b"mine\n*.log"
    assert rs.strip_host_excludes(b"/read (2).md\n", "sub") == b"/read (2).md\n"


def test_tab_less_reflog_lines_round_trip_byte_for_byte(tmp_path):
    repo = init(tmp_path / "ws")
    sha = commit(repo, {"a": "a\n"})
    git(repo, "update-ref", "refs/heads/plain", sha)
    raw = (tmp_path / "ws" / ".git" / "logs" / "refs" / "heads" / "plain").read_bytes()
    assert b"\t" not in raw
    state = state_of(repo)
    assert state.part("reflogs/refs/heads/plain") == raw
    entries, malformed, partial = rs.parse_reflog(raw)
    assert len(entries) == 1 and (malformed, partial) == (0, False)
    back = round_trip(state, tmp_path, os.path.join(repo, ".git", "objects"))
    assert back.state is not None and back.state.part("reflogs/refs/heads/plain") == raw


def test_the_reflog_grammar():
    a, b = "1" * 40, "2" * 40
    good = f"{a} {b} Ada <ada@x> 1700000000 +0100\tcommit: one".encode()
    spaced = f"{a} {b} A > B <weird> <ada@x> 1700000000 -0730".encode()      # ident ends at the last '>'
    bad = [f"{a} {b} Ada <ada@x> 17e9 +0100".encode(), f"{a} {b} Ada ada@x 1700000000 +0100".encode(),
           f"{'A' * 40} {b} Ada <a> 1 +0000".encode(), f"{a} {b} Ada <a> 1 +01".encode(), b"garbage"]
    body = b"\n".join([good, spaced, *bad, good]) + b"\n" + good[:30]
    entries, malformed, partial = rs.parse_reflog(body)
    assert [e.line for e in entries] == [good, spaced, good]
    assert (malformed, partial) == (5, True)
    assert entries[1].timestamp == 1700000000 and entries[0].old == a and entries[0].new == b


# --- config ---------------------------------------------------------------------------------------


_CORPUS = r"""[core]
	repositoryformatversion = 0
	bare = false
	hooksPath = /tmp/hooks
	fsmonitor = /tmp/monitor
	sshCommand = ssh -oProxyCommand=evil
	autocrlf = fal\
se
	editor = vim
[user]
	name = Ada
[pull]
	rebase
[branch "main"]
	remote = origin
	description = "line one\n[core]\n\thooksPath = /tmp/evil"
[Remote.FOO]
	url = https://example.com/foo.git
[remote "Origin"]
	url = https://example.com/upper.git
[remote "origin"]
	url = https://example.com/lower.git
	fetch = +refs/heads/*:refs/remotes/origin/*
	push = +refs/heads/*:refs/heads/*
	push = refs/heads/main:refs/heads/main
	mirror = true
	uploadpack = /tmp/evil
[include]
	path = /etc/evil
[includeIf "gitdir:/"]
	path = /etc/evil
[alias]
	x = !sh -c evil
[filter "lfs"]
	clean = evil %f
[diff "bin"]
	textconv = evil
[diff]
	algorithm = histogram
[http]
	extraHeader = Authorization: Basic c2VjcmV0
[url "https://evil/"]
	insteadOf = https://example.com/
[credential]
	helper = store
[color "diff"]
	meta = blue
[commit]
	gpgSign = true
[gpg]
	program = evil
[safe]
	directory = *
[protocol]
	allow = always
"""


def _config_of(repo):
    state = state_of(repo)
    body = state.part("config")
    return json.loads(body) if body else []


def test_the_config_corpus_keeps_only_allowed_keys(tmp_path):
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "a\n"})
    (tmp_path / "ws" / ".git" / "config").write_text(_CORPUS)
    pairs = _config_of(repo)
    assert pairs == sorted(pairs)
    assert all(rs.config_allowed(key, value) for key, value in pairs)
    keys = [key for key, _ in pairs]
    assert keys == [
        "branch.main.description", "branch.main.remote", "color.diff.meta", "core.autocrlf", "diff.algorithm",
        "pull.rebase", "remote.Origin.url", "remote.foo.url", "remote.origin.fetch", "remote.origin.push",
        "remote.origin.url", "user.name",
    ]
    assert dict(pairs)["core.autocrlf"] == "false"                       # the backslash continuation
    assert dict(pairs)["pull.rebase"] == "true"                          # a value-less boolean
    assert dict(pairs)["remote.origin.push"] == "refs/heads/main:refs/heads/main"
    assert dict(pairs)["branch.main.description"] == "line one\n[core]\n\thooksPath = /tmp/evil"
    flat = json.dumps([pair for pair in pairs if pair[0] != "branch.main.description"])
    for word in ("evil", "hooks", "monitor", "c2VjcmV0", "store", "mirror", "/etc/", "vim", "*"):
        assert word not in flat.replace("refs/heads/*:refs/remotes/origin/*", "")


def test_includes_are_never_followed(tmp_path):
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "a\n"})
    included = tmp_path / "included"
    included.write_text("[user]\n\tname = Included\n[core]\n\thooksPath = /x\n")
    git(repo, "config", "include.path", str(included))
    git(repo, "config", "includeIf.gitdir:/.path", str(included))
    pairs = _config_of(repo)
    assert "Included" not in json.dumps(pairs) and pairs == []


def test_the_config_match_rules():
    assert rs.config_allowed("Remote.origin.URL", "x")                       # section and variable: any case
    assert rs.config_allowed("remote.a.b.url", "x")                          # subsection between first and last dot
    assert not rs.config_allowed("core.x.autocrlf", "x")                     # core takes no subsection
    assert not rs.config_allowed("remote.url", "x")                          # remote needs one
    assert rs.config_allowed("color.ui", "auto") and rs.config_allowed("color.diff.meta", "blue")
    assert not rs.config_allowed("remote.o.push", "+refs/heads/*")
    assert not rs.config_allowed("remote.o.mirror", "true")
    assert not rs.config_allowed("diff.bin.textconv", "x")


def test_config_is_stored_sorted_whatever_its_file_order(tmp_path):
    """R4: config order is canonical, never file order."""
    hashes = []
    for name, refspecs in (("one", ["+refs/heads/a:refs/x/a", "+refs/heads/b:refs/x/b"]),
                           ("two", ["+refs/heads/b:refs/x/b", "+refs/heads/a:refs/x/a"])):
        repo = init(tmp_path / name)
        git(repo, "commit", "-q", "--allow-empty", "-m", "one", env={"GIT_AUTHOR_DATE": "@1700000000 +0000",
                                                                   "GIT_COMMITTER_DATE": "@1700000000 +0000"})
        os.remove(os.path.join(repo, ".git", "logs", "HEAD"))
        os.remove(os.path.join(repo, ".git", "logs", "refs", "heads", "main"))
        for refspec in refspecs:
            git(repo, "config", "--add", "remote.origin.fetch", refspec)
        hashes.append(state_of(repo).manifest_hash)
    assert hashes[0] == hashes[1]


def test_a_config_entry_that_is_not_utf8_is_dropped_with_a_line(tmp_path):
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "a\n"})
    with open(tmp_path / "ws" / ".git" / "config", "ab") as fh:
        fh.write(b"[user]\n\tname = Ad\xe9\n")
    done = read(repo)
    assert done.state is not None and "user.name" not in dict(json.loads(done.state.part("config") or b"[]"))
    assert "config: 1 entries not UTF-8 dropped" in done.dropped


def test_the_worktree_config_folds_in_and_its_extension_is_not_stored(tmp_path):
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "a\n"})
    git(repo, "config", "extensions.worktreeConfig", "true")
    (tmp_path / "ws" / ".git" / "config.worktree").write_text("[core]\n\tsparseCheckout = true\n")
    pairs = _config_of(repo)
    assert pairs == [["core.sparsecheckout", "true"]]


def test_a_token_in_any_url_or_message_leaves_no_trace(tmp_path):
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "a\n"})
    git(repo, "remote", "add", "origin", "https://u:tok@example.com/r.git?x=tok#tok")
    git(repo, "config", "remote.origin.pushurl", "https://tok@example.com/r.git")
    git(repo, "config", "branch.main.remote", "https://u:tok@example.com/r.git")
    git(repo, "config", "branch.main.description", "pushed with tok via https://ada:tok@host/x")
    git(repo, "update-ref", "-m", "fetch https://u:tok@example.com/r.git", "refs/remotes/origin/main", "HEAD")
    (tmp_path / "ws" / ".git" / "MERGE_MSG").write_text("Merge https://u:tok@example.com/r.git\n")
    (tmp_path / "ws" / ".git" / "MERGE_HEAD").write_text(git(repo, "rev-parse", "HEAD"))
    (tmp_path / "ws" / ".git" / "info" / "exclude").write_text("/from-https://u:tok@example.com/\n")
    done = read(repo, secrets=["tok"])
    assert done.state is not None
    for body in done.state.parts.values():
        assert b"tok" not in body
    config = dict(json.loads(done.state.part("config") or b"[]"))
    assert config["remote.origin.url"] == "https://example.com/r.git"
    assert config["remote.origin.pushurl"] == "https://example.com/r.git"
    assert config["branch.main.remote"] == "https://example.com/r.git"
    assert all("tok" not in line for line in done.dropped + done.warnings)
    assert "config remote.origin.url: credentials removed" in done.dropped
    assert "exclude: credentials removed" in done.dropped


def test_only_reflog_messages_lose_a_password_unbound_secrets_elsewhere_are_kept(tmp_path):
    """Rule 2 is for reflog messages: a patch or message a resumed operation
    applies keeps every byte unless it holds a bound account's secret (rule 3)."""
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "a\n"})
    git(repo, "update-ref", "-m", "fetch https://u:pw@example.com/r.git", "refs/remotes/origin/main", "HEAD")
    gitdir = tmp_path / "ws" / ".git"
    patch = b"+\turl = https://u:pw@example.com/x\n"
    (gitdir / "rebase-apply").mkdir()
    (gitdir / "rebase-apply" / "patch").write_bytes(patch)
    (gitdir / "MERGE_MSG").write_bytes(b"Merge https://u:pw@example.com/r.git\n")
    (gitdir / "MERGE_HEAD").write_text(git(repo, "rev-parse", "HEAD"))
    state = state_of(repo)
    assert b"https://u@example.com/r.git" in (state.part("reflogs/refs/remotes/origin/main") or b"")
    assert state.part("progress/rebase-apply/patch") == patch
    assert state.part("progress/MERGE_MSG") == b"Merge https://u:pw@example.com/r.git\n"
    assert b"pw" not in (state_of(repo, secrets=["pw"]).part("progress/rebase-apply/patch") or b"")


@pytest.mark.parametrize(("url", "clean"), [
    ("https://u:p@host/r.git", "https://host/r.git"),
    ("https://u@host/r.git?q=1#f", "https://host/r.git"),
    ("ssh://git:pw@host:22/r.git", "ssh://git@host:22/r.git"),
    ("ssh://git@host/r.git", "ssh://git@host/r.git"),
    ("git:pw@github.com:org/r.git", "git@github.com:org/r.git"),
    ("git@github.com:org/r.git", "git@github.com:org/r.git"),
    ("file:///srv/r.git", "file:///srv/r.git"),
    ("/srv/r.git", "/srv/r.git"),
    ("../r", "../r"),
])
def test_url_userinfo_rules(url, clean):
    assert rs.strip_url_userinfo(url) == clean


def test_ext_and_fd_remotes_are_dropped_and_local_ones_kept(tmp_path):
    pairs, lines = rs.sanitize_config([
        ("remote.evil.url", "ext::sh -c evil"), ("remote.evil.fetch", "+refs/heads/*:refs/remotes/evil/*"),
        ("remote.fd.pushurl", "FD::3"), ("branch.main.remote", "ext::sh"),
        ("remote.local.url", "/srv/r.git"), ("remote.file.url", "file:///srv/r.git"),
    ])
    assert pairs == [("remote.local.url", "/srv/r.git"), ("remote.file.url", "file:///srv/r.git")]
    assert len(lines) == 4 and not any("sh" in line.split(":", 1)[1] for line in lines)


# --- progress --------------------------------------------------------------------------------------


def _rebase_stopped_at_break(repo, todo_lines):
    """An interactive rebase stopped at a ``break`` with ``todo_lines`` still to do."""
    script = os.path.join(os.path.dirname(str(repo)), "editor.py")
    with open(script, "w") as fh:
        fh.write("import sys\nopen(sys.argv[1], 'w').write(" + repr("break\n" + "".join(todo_lines)) + ")\n")
    import sys
    git(repo, "rebase", "-i", "HEAD~1", env={"GIT_SEQUENCE_EDITOR": f"{sys.executable} {script}"})
    assert os.path.isdir(os.path.join(str(repo), ".git", "rebase-merge"))


def test_an_exec_todo_persists_no_operation_and_no_index(tmp_path):
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "1\n"})
    sha = commit(repo, {"a": "2\n"})
    _rebase_stopped_at_break(repo, [f"pick {sha} two\n", "exec touch pwned\n"])
    done = read(repo)
    assert done.state is not None
    names = done.state.manifest()["parts"]
    assert not [name for name in names if name.startswith("progress/") or name == "index"]
    assert done.warnings == ("rebase-merge/git-rebase-todo runs commands (exec); the operation is not kept",)


def test_a_rebase_without_exec_is_kept_whole(tmp_path):
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "1\n"})
    sha = commit(repo, {"a": "2\n"})
    _rebase_stopped_at_break(repo, [f"pick {sha} two\n", "# exec in a comment\n"])
    state = state_of(repo)
    progress = [n for n in state.manifest()["parts"] if n.startswith("progress/rebase-merge/")]
    assert "progress/rebase-merge/git-rebase-todo" in progress
    assert "progress/rebase-merge/interactive" in progress                 # an empty marker is state
    assert state.part("progress/rebase-merge/interactive") == b""


def test_a_comment_char_never_hides_an_exec(tmp_path):
    """Git takes the last core.commentChar; whichever it takes, an exec line
    is refused, so no comment character the reader might misjudge hides one."""
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "1\n"})
    sha = commit(repo, {"a": "2\n"})
    git(repo, "config", "--add", "core.commentChar", "e")
    git(repo, "config", "--add", "core.commentChar", ";")
    _rebase_stopped_at_break(repo, [f"pick {sha} two\n", "exec touch pwned\n"])
    done = read(repo)
    assert done.state is not None
    assert not [n for n in done.state.manifest()["parts"] if n.startswith("progress/") or n == "index"]
    assert done.warnings == ("rebase-merge/git-rebase-todo runs commands (exec); the operation is not kept",)


def test_progress_problems():
    exec_line = "rebase-merge/git-rebase-todo runs commands (exec); the operation is not kept"
    assert rs.progress_problem({"sequencer/todo": b"pick 1 a\n  x make\n"}) is not None
    assert rs.progress_problem({"sequencer/todo": b"; exec make\n# exec make\npick 1\n"}) is None
    assert rs.progress_problem({"rebase-merge/git-rebase-todo": b"exec make\n"}) == exec_line
    assert rs.progress_problem({"rebase-merge/git-rebase-todo": b"\texec\tmake\r\n"}) == exec_line
    assert rs.progress_problem({"rebase-merge/strategy": b"ort\n"}) is None
    assert rs.progress_problem({"rebase-merge/strategy": b"evil\n"}) is not None
    assert rs.progress_problem({"rebase-merge/strategy": b"ort \n"}) is not None
    assert rs.progress_problem({"rebase-merge/gpg_sign_opt": b"-S"}) is not None


@pytest.mark.parametrize(("opts", "kept"), [
    (b"[options]\n\tstrategy = ort\n\tstrategy-option = theirs\n", True),
    (b"[options]\n\tstrategy-option = \"a;b\"\n\tstrategy-option = \" sp\"\n\tmainline = 1\n", True),
    (b"[options]\n\tstrategy = evil\n", False),
    (b"[options]\n\tstrategy = ort\n\tstrategy = evil\n", False),
    (b"[options]\n\tgpg-sign = -Skey\n", False),                           # never stored
    (b"[options]\n\tstrategy = \"ev\\\nil\"\n", False),                     # not how git writes it
    (b"[options]\n\tstrategy = ev\\\nil\n", False),                         # a continuation
    (b"[options]\n\tStrategy = evil\n", False),
    (b"[options]\n\tstrategy=evil\n", False),
    (b"[options \"x\"]\n\tstrategy = ort\n", False),
    (b"\tstrategy = ort\n", False),                                         # no header
    (b"[options]\n\tstrategy = ort", False),                                # no final newline
    (b"[options]\n\tstrategy = ort ; evil\n", False),
])
def test_the_sequencer_options_recheck_is_pure_and_strict(opts, kept):
    assert (rs.progress_problem({"sequencer/opts": opts}) is None) is kept


def test_a_cherry_pick_sequence_keeps_the_options_git_wrote(tmp_path):
    repo = init(tmp_path / "ws")
    commit(repo, {"f": "a\n"})
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, {"f": "b\n"})
    commit(repo, {"g": "g\n"})
    git(repo, "checkout", "-q", "main")
    commit(repo, {"f": "c\n"})
    git(repo, "cherry-pick", "-x", "--strategy=recursive", "-X", "a;b", "-X", " sp", "-X", "find-renames=50%",
        "--allow-empty", "main..side", check=False)
    opts = (tmp_path / "ws" / ".git" / "sequencer" / "opts").read_bytes()
    assert rs.sequencer_options(opts) is not None
    state = state_of(repo)
    assert state.part("progress/sequencer/opts") == opts and state.part("progress/sequencer/todo") is not None


def test_signing_options_are_never_stored(tmp_path):
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "1\n"})
    gitdir = tmp_path / "ws" / ".git"
    (gitdir / "sequencer").mkdir()
    (gitdir / "sequencer" / "opts").write_text("[options]\n\tstrategy = ort\n\tgpg-sign = -Skey\n")
    (gitdir / "sequencer" / "head").write_text(git(repo, "rev-parse", "HEAD"))
    (gitdir / "rebase-merge").mkdir()
    (gitdir / "rebase-merge" / "gpg_sign_opt").write_text("-Skey")
    (gitdir / "rebase-merge" / "head-name").write_text("refs/heads/main\n")
    state = state_of(repo)
    names = state.manifest()["parts"]
    assert "progress/rebase-merge/gpg_sign_opt" not in names
    opts = state.part("progress/sequencer/opts")
    assert opts is not None and b"gpg" not in opts and b"strategy = ort" in opts
    (gitdir / "sequencer" / "opts").write_text("[options]\n\tstrategy = evil\n")
    assert not [n for n in state_of(repo).manifest()["parts"] if n.startswith("progress/")]


# --- the operation index ---------------------------------------------------------------------------


def test_a_merge_conflict_index_round_trips(tmp_path):
    repo = conflicted(tmp_path)
    before = git(repo, "ls-files", "-s")
    status = git(repo, "status", "--porcelain")
    assert {"UU f", "A  added", "D  gone", "M  clean"} <= set(status.splitlines())
    state = state_of(repo)
    index = state.part("index")
    assert index is not None
    lines = [line.decode() for line in index.split(b"\0") if line]
    null = "0 " + "0" * 40
    assert f"{null}\tf" in lines and f"{null}\tgone" in lines
    assert [line.split(" ")[2].split("\t")[0] for line in lines if line.endswith("\tf") and line[0] != "0"] == \
        ["1", "2", "3"]
    assert sum(line.endswith("\tadded") for line in lines) == 2                 # removal, then the entry
    target = str(tmp_path / "replayed")
    os.makedirs(target)
    git(target, "init", "-q", "--bare")
    with open(os.path.join(target, "objects", "info", "alternates"), "w") as fh:
        fh.write(os.path.join(repo, ".git", "objects") + "\n")
    env = {"GIT_INDEX_FILE": os.path.join(target, "index.replay")}
    git(target, "read-tree", git(repo, "rev-parse", "HEAD").strip(), env=env)
    git(target, "update-index", "-z", "--index-info", input=index, env=env)
    assert git(target, "ls-files", "-s", env=env) == before


def test_a_split_index_round_trips(tmp_path):
    """The shared index lives beside ``index``; 2.31 looks for it in the
    gitdir, later git beside the index too."""
    repo = init(tmp_path / "ws")
    commit(repo, {"f": "base\n"})
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, {"f": "side\n"})
    git(repo, "checkout", "-q", "main")
    commit(repo, {"f": "main\n"})
    git(repo, "update-index", "--split-index")
    git(repo, "merge", "side", check=False)
    gitdir = tmp_path / "ws" / ".git"
    assert [name for name in os.listdir(gitdir) if name.startswith("sharedindex.")]
    before = git(repo, "ls-files", "-s")
    done = read(repo)
    assert done.state is not None and done.warnings == ()
    assert done.state.part("index") is not None
    write_gitdir(done.state, str(tmp_path / "back" / ".git"), str(gitdir / "objects"))
    assert git(tmp_path / "back", "ls-files", "-s") == before


def test_an_unreadable_index_in_an_operation_warns_that_resolutions_must_be_re_added(tmp_path, monkeypatch):
    repo = conflicted(tmp_path)
    index = tmp_path / "ws" / ".git" / "index"
    original = index.read_bytes()
    index.write_bytes(b"DIRC garbage")
    done = read(repo)
    assert done.state is not None and done.state.part("index") is None
    assert done.state.part("progress/MERGE_HEAD") is not None
    [warning] = done.warnings
    assert warning.startswith("index unreadable (") and warning.endswith(f"; {rs.STAGED_RESOLUTIONS}")
    assert "git-index-" not in warning and str(tmp_path) not in warning
    index.write_bytes(original)
    monkeypatch.setattr(rs, "READ_LIMIT", len(original) - 1)
    done = read(repo)
    assert done.state is not None and done.state.part("index") is None
    assert done.warnings == (f"index over 64 MiB not read; {rs.STAGED_RESOLUTIONS}",)


def test_an_intent_to_add_path_stays_untracked(tmp_path):
    repo = conflicted(tmp_path)
    (tmp_path / "ws" / "later").write_text("not yet\n")
    git(repo, "add", "-N", "later")
    assert "later" in git(repo, "ls-files")
    index = state_of(repo).part("index")
    assert index is not None and b"later" not in index


def test_outside_an_operation_the_index_is_not_kept(tmp_path):
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "1\n"})
    (tmp_path / "ws" / "a").write_text("staged\n")
    git(repo, "add", "a")
    assert state_of(repo).part("index") is None


def test_stash_pop_conflicts_keep_the_index_without_merge_head(tmp_path):
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "1\n"})
    (tmp_path / "ws" / "a").write_text("stashed\n")
    git(repo, "stash", "-q")
    commit(repo, {"a": "committed\n"})
    git(repo, "stash", "pop", check=False)
    state = state_of(repo)
    assert "progress/MERGE_HEAD" not in state.manifest()["parts"]
    assert state.part("index") is not None
    assert state.part("reflogs/refs/stash") is not None


def test_the_ls_files_debug_parser_reads_flags():
    out = (b"100644 " + b"a" * 40 + b" 0\tita\0  ctime: 0:0\n  mtime: 0:0\n  dev: 0\tino: 0\n"
           b"  uid: 0\tgid: 0\n  size: 0\tflags: 20004000\n"
           b"160000 " + b"b" * 40 + b" 2\tsub\nmodule\0  ctime: 1:2\n  mtime: 3:4\n  dev: 5\tino: 6\n"
           b"  uid: 7\tgid: 8\n  size: 0\tflags: 2000\n")
    entries = rs.parse_ls_files_debug(out)
    assert [(e.mode, e.stage, e.path, e.flags) for e in entries] == [
        ("100644", 0, b"ita", 0x20004000), ("160000", 2, b"sub\nmodule", 0x2000)]


# --- expiry ----------------------------------------------------------------------------------------


def test_reflog_expiry_by_gits_defaults(tmp_path):
    repo = init(tmp_path / "ws")
    c1 = commit(repo, {"a": "1\n"})
    c2 = commit(repo, {"a": "2\n"})
    git(repo, "checkout", "-q", "-b", "lost")
    lost = commit(repo, {"a": "lost\n"})
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-q", "-D", "lost")
    null = "0" * 40

    def line(old, new, age, msg):
        return f"{old} {new} Ada <ada@example.com> {NOW - age * DAY} +0000\t{msg}\n"

    log = "".join([
        line(null, c1, 100, "old reachable"),           # past 90 days: goes
        line(c1, c2, 40, "reachable at 40"),            # 30-90 days, reachable: stays
        line(c2, lost, 40, "unreachable at 40"),        # 30-90 days, unreachable: goes
        line(lost, c2, 10, "unreachable at 10"),        # under 30 days: stays
    ])
    gitdir = tmp_path / "ws" / ".git"
    (gitdir / "logs" / "refs" / "heads" / "main").write_text(log)
    (gitdir / "logs" / "HEAD").write_text(log)
    git(repo, "update-ref", "refs/stash", c2)
    (gitdir / "logs" / "refs" / "stash").write_text(line(null, c1, 400, "ancient stash") + line(c1, c2, 200, "s"))
    done = read(repo, now=NOW)
    assert done.state is not None
    kept = done.state.part("reflogs/refs/heads/main") or b""
    assert [m.split("\t")[1] for m in kept.decode().splitlines()] == ["reachable at 40", "unreachable at 10"]
    assert done.state.part("reflogs/HEAD") == kept                   # HEAD: reachable from any ref
    stash = done.state.part("reflogs/refs/stash") or b""
    assert len(stash.splitlines()) == 2                                # refs/stash never expires
    assert "reflog refs/heads/main: 2 expired entries dropped" in done.dropped
    again = read(repo, now=NOW)
    assert again.state is not None and again.state.manifest_hash == done.state.manifest_hash


# --- the format gate -------------------------------------------------------------------------------


def _gate_repo(tmp_path):
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "1\n"})
    return repo, tmp_path / "ws" / ".git"


@pytest.mark.parametrize(("setup", "message"), [
    (lambda r, g: git(r, "config", "core.repositoryformatversion", "2"), "repository format version 2 is not kept"),
    (lambda r, g: git(r, "config", "extensions.refStorage", "reftable"), rs.REFTABLE),
    (lambda r, g: (g / "reftable").mkdir(), rs.REFTABLE),
    (lambda r, g: (g / "HEAD").write_text("ref: refs/heads/.invalid\n"), rs.REFTABLE),
    (lambda r, g: [os.rename(g / "refs" / "tags", g / "t"), (g / "refs" / "tags").write_text("")], rs.REFTABLE),
    (lambda r, g: git(r, "config", "extensions.objectFormat", "sha256"), rs.SHA256),
    (lambda r, g: git(r, "config", "extensions.partialClone", "origin"), rs.PARTIAL),
    (lambda r, g: git(r, "config", "remote.origin.promisor", "true"), rs.PARTIAL),
    (lambda r, g: git(r, "config", "remote.origin.partialclonefilter", "blob:none"), rs.PARTIAL),
    (lambda r, g: (g / "objects" / "pack" / "pack-1.promisor").write_text(""), rs.PARTIAL),
    (lambda r, g: (g / "objects" / "info" / "alternates").write_text("/elsewhere/objects\n"), rs.BORROWS),
    (lambda r, g: git(r, "config", "extensions.somethingNew", "true"), "extensions.somethingnew is not kept"),
])
def test_each_format_gate_refusal(tmp_path, setup, message):
    repo, gitdir = _gate_repo(tmp_path)
    setup(repo, gitdir)
    done = read(repo)
    assert (done.state, done.refusal) == (None, message)


def test_the_gate_passes_what_it_keeps(tmp_path):
    repo, gitdir = _gate_repo(tmp_path)
    git(repo, "config", "core.repositoryformatversion", "1")                  # 2.31's sparse-checkout set (P13)
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "config", "extensions.preciousObjects", "true")
    store = str(tmp_path / "store.git" / "objects")
    (gitdir / "objects" / "info" / "alternates").write_text(store + "\n")
    done = read(repo, alternates=store)
    assert done.refusal is None
    (gitdir / "objects" / "info" / "alternates").unlink()
    origin = init(tmp_path / "origin")
    commit(origin, {"a": "1\n"})
    commit(origin, {"a": "2\n"})
    subprocess.run(["git", "clone", "-q", "--depth=1", f"file://{origin}", str(tmp_path / "shallow")],
                   check=True, env=git_env(str(tmp_path)), capture_output=True)
    shallow = state_of(tmp_path / "shallow")                                   # shallow passes (D20)
    assert shallow.part("shallow") == (tmp_path / "shallow" / ".git" / "shallow").read_bytes()


# --- the reader's rules ----------------------------------------------------------------------------


def test_loose_refs_win_over_packed_and_bad_values_keep_their_base(tmp_path):
    repo = init(tmp_path / "ws")
    c1 = commit(repo, {"a": "1\n"})
    c2 = commit(repo, {"a": "2\n"})
    git(repo, "branch", "other", c1)
    git(repo, "branch", "broken", c1)
    git(repo, "pack-refs", "--all")
    base = state_of(repo)
    gitdir = tmp_path / "ws" / ".git"
    (gitdir / "refs" / "heads" / "other").write_text(c2 + "\n")                 # loose wins
    (gitdir / "refs" / "heads" / "broken").write_text("not a sha\n")            # base value kept
    (gitdir / "refs" / "heads" / "new-bad").write_text("nope\n")                # no base: dropped
    (gitdir / "refs" / "heads" / "held.lock").write_text(c2 + "\n")             # skipped
    (gitdir / "refs" / "gc-roots").mkdir()
    (gitdir / "refs" / "gc-roots" / "x").write_text(c2 + "\n")                  # ignored
    (gitdir / "refs" / "heads" / "linked").symlink_to(gitdir / "refs" / "heads" / "main")
    with open(gitdir / "packed-refs", "a") as fh:
        fh.write(f"^{c1}\n{c1} refs/heads/bad..name\nnot a line\n")
    done = read(repo, base=base)
    assert done.state is not None
    refs = rs.parse_refs(done.state.part("refs") or b"")
    assert refs == {"refs/heads/main": c2, "refs/heads/other": c2, "refs/heads/broken": c1}
    assert "ref refs/heads/broken has a bad value; its base value is kept" in done.dropped
    assert "ref refs/heads/new-bad dropped (no base value)" in done.dropped
    assert "ref refs/gc-roots/x ignored" in done.dropped
    assert "a malformed packed-refs line dropped" in done.dropped


def test_refs_and_reflogs_named_like_temporary_files_are_kept(tmp_path):
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "1\n"})
    for branch in ("tmp_fix", "notes.tmp"):
        git(repo, "branch", branch)
    git(repo, "tag", "v1.tmp")
    gitdir = tmp_path / "ws" / ".git"
    (gitdir / "tmp_obj_x").write_text("x")
    done = read(repo)
    assert done.state is not None
    names = done.state.manifest()["parts"]
    refs = rs.parse_refs(done.state.part("refs") or b"")
    assert {"refs/heads/tmp_fix", "refs/heads/notes.tmp", "refs/tags/v1.tmp"} <= set(refs)
    assert {"reflogs/refs/heads/tmp_fix", "reflogs/refs/heads/notes.tmp"} <= set(names)


def test_a_packed_refs_file_over_1_mib_whose_refs_fit_is_kept(tmp_path, monkeypatch):
    """packed-refs holds peeled lines the refs part never does: its cap is
    the read limit, and the 1 MiB cap is the built part's."""
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "1\n"})
    git(repo, "tag", "-a", "-m", "t", "t")
    tag = git(repo, "rev-parse", "refs/tags/t").strip()
    peeled = git(repo, "rev-parse", "HEAD").strip()
    with open(tmp_path / "ws" / ".git" / "packed-refs", "w") as fh:
        fh.write("# pack-refs with: peeled fully-peeled sorted \n")
        fh.writelines(f"{tag} refs/tags/t{n:05}\n^{peeled}\n" for n in range(12000))
    assert os.path.getsize(tmp_path / "ws" / ".git" / "packed-refs") > rs.PART_CAP
    done = read(repo)
    assert done.refusal is None and done.state is not None
    assert len(rs.parse_refs(done.state.part("refs") or b"")) == 12001 + 1     # the tags, t, main
    monkeypatch.setattr(rs, "READ_LIMIT", rs.PART_CAP)
    assert read(repo).refusal == "refs over 1 MiB"


def test_packed_names_outside_refs_are_dropped_and_ignored_refs_logged_once(tmp_path):
    repo = init(tmp_path / "ws")
    sha = commit(repo, {"a": "1\n"})
    git(repo, "update-ref", "-m", "keep", "refs/prefetch/remotes/origin/main", sha)
    with open(tmp_path / "ws" / ".git" / "packed-refs", "w") as fh:
        fh.write(f"{sha} foo/bar\n")
    done = read(repo)
    assert done.state is not None
    assert set(rs.parse_refs(done.state.part("refs") or b"")) == {"refs/heads/main"}
    assert "a ref with a bad name dropped" in done.dropped
    assert done.dropped.count("ref refs/prefetch/remotes/origin/main ignored") == 1


def test_a_bad_head_keeps_its_base_or_refuses_without_one(tmp_path):
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "1\n"})
    base = state_of(repo)
    (tmp_path / "ws" / ".git" / "HEAD").write_text("garbage\n")
    kept = read(repo, base=base)
    assert kept.state is not None and kept.state.manifest()["head"] == "ref: refs/heads/main"
    assert read(repo).refusal == "HEAD unreadable"


@pytest.mark.parametrize("name", ["refs/heads/a b", "refs/heads/x.lock/y", "refs/heads/.hidden", "refs/heads/a@{1}",
                                  "refs/heads/end.", "HEAD", "refs//x", "refs/heads/a~1", "refs/heads/q?"])
def test_bad_ref_names(name):
    assert not rs.check_ref_format(name)


@pytest.mark.parametrize("name", ["refs/heads/main", "refs/stash", "refs/pull/1/head", "refs/heads/ünï",
                                  "refs/remotes/origin/HEAD"])
def test_good_ref_names(name):
    assert rs.check_ref_format(name)


def test_an_unreadable_gitdir_entry_refuses(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "1\n"})
    locked = tmp_path / "ws" / ".git" / "refs" / "heads" / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        done = read(repo)
    finally:
        locked.chmod(0o755)
    assert done.refusal is not None and done.refusal.startswith("gitdir unreadable: refs/heads/locked")


def test_a_symlinked_gitdir_is_refused_by_the_workspace_reader(tmp_path):
    real = init(tmp_path / "real")
    commit(real, {"a": "1\n"})
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git").symlink_to(tmp_path / "real" / ".git")
    got = rs.read_repository(str(ws), "")
    assert got.refusal is not None and got.refusal.startswith("gitdir unreadable")
    nested = rs.read_repository(str(tmp_path), "real")
    assert nested.refusal is None and nested.head == "ref: refs/heads/main"


def test_hooks_hookspath_and_worktrees_are_not_kept_with_a_line(tmp_path):
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "1\n"})
    gitdir = tmp_path / "ws" / ".git"
    (gitdir / "hooks").mkdir(exist_ok=True)
    (gitdir / "hooks" / "pre-commit").write_text("#!/bin/sh\n")
    (gitdir / "worktrees" / "wt").mkdir(parents=True)
    git(repo, "config", "core.hooksPath", "/x")
    done = read(repo)
    assert {"not kept: hooks", "not kept: worktrees", "not kept: core.hooksPath"} <= set(done.dropped)


# --- the root filter -------------------------------------------------------------------------------


def test_missing_roots(tmp_path):
    repo = conflicted(tmp_path)
    gitdir = tmp_path / "ws" / ".git"
    ghost = "f" * 40
    with open(gitdir / "logs" / "HEAD", "a") as fh:
        fh.write(f"{ghost} {ghost} Ada <a@b> {int(time.time())} +0000\tghost\n")
    (gitdir / "CHERRY_PICK_HEAD").write_text(ghost + "\n")
    done = read(repo)
    assert done.state is not None
    assert "progress/CHERRY_PICK_HEAD" not in done.state.manifest()["parts"]
    assert "progress/MERGE_HEAD" in done.state.manifest()["parts"]
    assert b"ghost" not in (done.state.part("reflogs/HEAD") or b"")
    git(repo, "update-ref", "--no-deref", "refs/heads/dangling", git(repo, "rev-parse", "HEAD").strip())
    (gitdir / "refs" / "heads" / "dangling").write_text(ghost + "\n")
    assert read(repo).refusal == "history incomplete: refs/heads/dangling names a missing object"


class FakeObjects:
    """Every sha present, every candidate reachable; a fixed index and tree."""

    def __init__(self, entries=(), tree=(), missing=()):
        self.entries, self.tree_entries, self.missing = list(entries), list(tree), set(missing)

    def exists(self, shas):
        return {sha: "commit" for sha in shas if sha not in self.missing}

    def unreachable(self, candidates, tips):
        return set()

    def tree(self, commit):
        return self.tree_entries

    def index(self, body, shared):
        return self.entries


def _bare_read(**fields):
    sha = "a" * 40
    base = {"head": "ref: refs/heads/main", "refs": {"refs/heads/main": sha}}
    base.update(fields)
    return rs.GitdirRead("", **base)


def test_a_missing_index_sha_drops_the_index_alone(tmp_path):
    sha = "a" * 40
    entries = [rs.IndexEntry("100644", "b" * 40, stage, b"f", 0) for stage in (1, 2, 3)]
    got = _bare_read(progress={"MERGE_HEAD": f"{sha}\n".encode()}, index=b"raw")
    done = rs.normalize(got, FakeObjects(entries, missing={"b" * 40}))
    assert done.state is not None
    assert done.state.part("index") is None and done.state.part("progress/MERGE_HEAD") is not None
    assert done.warnings == (f"index: objects missing; {rs.STAGED_RESOLUTIONS}",)


# --- caps ------------------------------------------------------------------------------------------


def test_an_index_over_its_cap_is_dropped_alone():
    sha = "a" * 40
    entries = [rs.IndexEntry("100644", "b" * 40, stage, f"dir/{n:06}".encode() * 10, 0)
               for n in range(3000) for stage in (1, 2, 3)]
    got = _bare_read(progress={"MERGE_HEAD": f"{sha}\n".encode()}, index=b"raw")
    done = rs.normalize(got, FakeObjects(entries))
    assert done.state is not None
    assert done.state.part("index") is None and done.state.part("progress/MERGE_HEAD") is not None
    assert done.warnings == (f"index over 1 MiB; {rs.STAGED_RESOLUTIONS}",)


def test_a_reflog_over_its_cap_keeps_its_newest_lines_and_names_lost_stashes():
    a = "a" * 40
    entries = [rs.ReflogEntry(a, a, NOW, f"{a} {a} Ada <a@b> {NOW} +0000\t{n:08} {'x' * 200}".encode())
               for n in range(20000)]
    got = _bare_read(reflogs={"refs/heads/main": entries, "refs/stash": entries}, refs={"refs/heads/main": a,
                                                                                         "refs/stash": a})
    done = rs.normalize(got, FakeObjects(), now=NOW)
    assert done.state is not None
    body = done.state.part("reflogs/refs/heads/main") or b""
    assert len(body) <= rs.REFLOG_CAP and body.endswith(entries[-1].line + b"\n")
    kept = len(body.splitlines())
    assert body.splitlines()[0] == entries[20000 - kept].line
    lost = 20000 - kept
    assert f"reflog refs/heads/main over 4 MiB; its {lost} oldest entries dropped" in done.warnings
    stash_lines = [line for line in done.dropped if line.startswith("refs/stash: stash@{")]
    assert len(stash_lines) == lost and stash_lines[0] == "refs/stash: stash@{19999} dropped (reflog over 4 MiB)"


def test_refs_or_config_over_their_cap_refuse():
    a = "a" * 40
    many = {f"refs/tags/t{n:06}": a for n in range(30000)}
    assert rs.normalize(_bare_read(refs={"refs/heads/main": a, **many}), FakeObjects()).refusal == \
        "refs over 1 MiB"
    big = [(f"remote.r{n}.url", "https://example.com/" + "x" * 100) for n in range(10000)]
    assert rs.normalize(_bare_read(config=big), FakeObjects()).refusal == "config over 1 MiB"


def test_an_oversized_operation_file_drops_its_whole_set(tmp_path):
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "1\n"})
    sha = commit(repo, {"a": "2\n"})
    _rebase_stopped_at_break(repo, [f"pick {sha} two\n"])
    gitdir = tmp_path / "ws" / ".git"
    (gitdir / "rebase-merge" / "message").write_bytes(b"x" * (rs.PART_CAP + 1))
    (gitdir / "SQUASH_MSG").write_bytes(b"y" * (rs.PART_CAP + 1))
    (gitdir / "MERGE_MSG").write_text("small\n")
    done = read(repo)
    assert done.state is not None
    names = done.state.manifest()["parts"]
    assert not [n for n in names if n.startswith("progress/rebase-merge/")]
    assert "progress/SQUASH_MSG" not in names and "progress/MERGE_MSG" in names
    assert "progress/rebase-merge/message over 1 MiB; rebase-merge not kept" in done.warnings
    assert "progress/SQUASH_MSG over 1 MiB; SQUASH_MSG not kept" in done.warnings


def test_a_config_file_over_its_cap_refuses(tmp_path):
    repo = init(tmp_path / "ws")
    commit(repo, {"a": "1\n"})
    with open(tmp_path / "ws" / ".git" / "config", "a") as fh:
        fh.write("# " + "x" * rs.PART_CAP + "\n")
    assert read(repo).refusal == "config over 1 MiB"


# --- roots ------------------------------------------------------------------------------------------


def test_the_roots_parser_over_every_source():
    h = {name: f"{n:040x}" for n, name in enumerate(
        ["ref", "head", "old", "new", "merge1", "merge2", "orig", "pick", "revert", "rebase", "auto", "autostash",
         "bisect", "onto", "stopped", "amend", "apply", "seq", "shallow", "blob", "gitlink", "abbrev", "msg"], 1)}
    null = "0" * 40
    parts = {
        "refs": f"{h['ref']} refs/heads/main\nref: refs/heads/main refs/remotes/origin/HEAD\n".encode(),
        "reflogs/HEAD": f"{null} {h['old']} A <a> 1 +0000\n{h['old']} {h['new']} A <a> 2 +0000\tm\n".encode(),
        "progress/MERGE_HEAD": f"{h['merge1']}\n{h['merge2']}\n".encode(),
        "progress/ORIG_HEAD": f"{h['orig']}\n".encode(),
        "progress/CHERRY_PICK_HEAD": f"{h['pick']}\n".encode(),
        "progress/REVERT_HEAD": f"{h['revert']}\n".encode(),
        "progress/REBASE_HEAD": f"{h['rebase']}\n".encode(),
        "progress/AUTO_MERGE": f"{h['auto']}\n".encode(),
        "progress/MERGE_AUTOSTASH": f"{h['autostash']}\n".encode(),
        "progress/BISECT_HEAD": f"{h['bisect']}\n".encode(),
        "progress/rebase-merge/onto": f"{h['onto']}\n".encode(),
        "progress/rebase-merge/stopped-sha": f"{h['stopped']}\n".encode(),
        "progress/rebase-merge/amend": f"{h['amend']}\n".encode(),
        "progress/rebase-apply/abort-safety": f"{h['apply']}\n".encode(),
        "progress/sequencer/head": f"{h['seq']}\n".encode(),
        "progress/rebase-merge/done": f"pick {h['msg']} not a root\n".encode(),
        "progress/MERGE_MSG": f"Merge {h['msg']}\n".encode(),
        "progress/BISECT_LOG": f"git bisect good {h['msg']}\n".encode(),
        "progress/rebase-merge/orig-head": h["abbrev"][:12].encode(),
        "shallow": f"{h['shallow']}\n".encode(),
        "index": b"\0".join(line.encode() for line in [
            f"0 {null}\tf", f"100644 {h['blob']} 1\tf", f"160000 {h['gitlink']} 0\tsub", ""]),
    }
    state = rs.make_state("ref: refs/heads/main", parts)
    expected = set(h.values()) - {h["head"], h["gitlink"], h["abbrev"], h["msg"]}
    assert rs.roots(state) == expected
    detached = rs.make_state(h["head"], {})
    assert rs.roots(detached) == {h["head"]}


def test_empty_parts_are_omitted_but_empty_progress_files_kept():
    state = rs.make_state("ref: refs/heads/main", {"refs": b"", "progress/MERGE_MODE": b"", "config": b"[]"})
    assert set(state.manifest()["parts"]) == {"progress/MERGE_MODE", "config"}
    assert state.manifest_hash in state.parts


# --- discovery -------------------------------------------------------------------------------------


def test_discovery_finds_exact_dotgit_directories_and_reports_the_unusable(tmp_path):
    ws = tmp_path / "ws"
    for repo in ("", "a", "a/b/c", "node_modules/x", ".collrun/y", "_runs/z", "deep/.venv/q", "sub/__pycache__"):
        (ws / repo / ".git").mkdir(parents=True)
    (ws / "a" / ".git" / "inner" / ".git").mkdir(parents=True)                 # inside a gitdir: never looked at
    (ws / "gitfile").mkdir()
    (ws / "gitfile" / ".git").write_text("gitdir: ../elsewhere\n")
    (ws / "linked").mkdir()
    (ws / "linked" / ".git").symlink_to(ws / "a" / ".git")
    (ws / "dotGIT" / ".GIT").mkdir(parents=True)
    (ws / "elsewhere").symlink_to(tmp_path)
    (ws / "piped").mkdir()
    os.mkfifo(ws / "piped" / ".git")
    found = rs.discover(str(ws))
    assert found.repositories == ("", "a", "a/b/c")
    assert found.unusable == {"gitfile": "not kept: submodule gitfile (gitfile)",
                              "linked": "not kept: linked/.git is a symlink",
                              "piped": "not kept: piped/.git is not a directory"}
    assert found.errors == ()


def test_an_unreadable_dotgit_is_reported_not_read(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    ws = tmp_path / "ws"
    (ws / "a" / ".git").mkdir(parents=True)
    (ws / "b" / ".git").mkdir(parents=True)
    (ws / "a" / ".git").chmod(0)
    try:
        found = rs.discover(str(ws))
    finally:
        (ws / "a" / ".git").chmod(0o755)
    assert found.repositories == ("b",)
    assert found.unusable == {"a": "not kept: a/.git is unreadable (Permission denied)"} and found.errors == ()


def test_discovery_stops_at_its_depth_and_reports_it(tmp_path):
    ws = tmp_path / "ws"
    deep = ws.joinpath(*["d"] * (rs.MAX_DEPTH + 2))
    (deep / ".git").mkdir(parents=True)
    found = rs.discover(str(ws))
    assert found.repositories == ()
    assert len(found.errors) == 1 and "deeper than 64 directories" in found.errors[0]


def test_the_repository_cap_keeps_rows_then_the_shallowest():
    paths = [f"r{n:03}" for n in range(300)] + ["a/b", ""]
    kept, overflow = rs.cap_repositories(paths, existing=["r299", "a/b"])
    assert overflow == 302 - rs.MAX_REPOSITORIES_PER_CELL
    assert kept[:3] == ["r299", "a/b", ""] and len(kept) == rs.MAX_REPOSITORIES_PER_CELL
    assert kept[3:] == [f"r{n:03}" for n in range(rs.MAX_REPOSITORIES_PER_CELL - 3)]
