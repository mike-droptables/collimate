"""build/push_public.py: the staged public tree, its exclude and presence
assertions, the secret scan, and a dry run and a push against local repos.

Secret-shaped values are assembled from parts, so this file itself passes the
scan it tests."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location('push_public', ROOT / 'build/push_public.py')
push_public = importlib.util.module_from_spec(_SPEC)
sys.modules['push_public'] = push_public
_SPEC.loader.exec_module(push_public)

EXCLUDES = '# private paths\n\n/docs/\n/llm/\n/.claude/\n/AGENTS.md\n/sdk/llm/\n'


def _git(cwd, *args, **kwargs):
    return subprocess.run(['git', '-c', 'commit.gpgsign=false', '-c', 'tag.gpgsign=false', *args], cwd=cwd,
                          check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def _write(root, files):
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)


def _private(tmp_path, extra=None):
    """A private checkout pushed to a bare origin: the paths the exclude list
    drops, a nested docs/ folder it keeps, and the files the tool reads."""
    origin = tmp_path / 'origin.git'
    _git(tmp_path, 'init', '--quiet', '--bare', '-b', 'main', str(origin))
    repo = tmp_path / 'private'
    repo.mkdir()
    _git(repo, 'init', '--quiet', '-b', 'main')
    _git(repo, 'config', 'user.name', 'Owner')
    _git(repo, 'config', 'user.email', 'owner@example.invalid')
    _write(repo, {
        '.github/sync-exclude.txt': EXCLUDES,
        'docs/plan.md': 'private plan', 'docs/skills/run/SKILL.md': 'skill', 'llm/prompt.md': 'scratch',
        'AGENTS.md': 'rules', 'sdk/llm/notes.md': 'scratch',
        'site/docs/history.md': 'published history', 'sdk/shared/docs/guide.md': 'the guide',
        'README.md': 'readme', 'api/pyproject.toml': '[project]\nname="collimate"\nversion="1.2.3"\n',
        'sdk/pyproject.toml': '[project]\nname="collimate-sdk"\nversion="1.2.3"\n',
        'desktop/package.json': '{"version": "1.2.3"}', 'run.sh': '#!/bin/sh\necho run\n',
        **(extra or {})})
    (repo / 'build').mkdir(exist_ok=True)
    shutil.copyfile(ROOT / 'build/release_policy.py', repo / 'build/release_policy.py')
    (repo / 'run.sh').chmod(0o755)
    (repo / '.claude/skills').mkdir(parents=True)
    (repo / '.claude/skills/run').symlink_to('../../docs/skills/run')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '--quiet', '-m', 'private history')
    _git(repo, 'remote', 'add', 'origin', str(origin))
    _git(repo, 'push', '--quiet', 'origin', 'main')
    return repo


def _public(tmp_path):
    """The public repo: a bare repository whose main holds an unrelated site."""
    public = tmp_path / 'public.git'
    _git(tmp_path, 'init', '--quiet', '--bare', '-b', 'main', str(public))
    work = tmp_path / 'public-work'
    _git(tmp_path, 'clone', '--quiet', str(public), str(work))
    _write(work, {'index.html': 'old site'})
    _git(work, 'add', '-A')
    _git(work, '-c', 'user.name=Site', '-c', 'user.email=site@example.invalid', 'commit', '--quiet', '-m', 'site')
    _git(work, 'push', '--quiet', 'origin', 'HEAD:main')
    return public


@pytest.fixture(autouse=True)
def own_git_config(tmp_path, monkeypatch):
    """The tool runs under this test's git configuration alone (no signing,
    hooks or URL rewrites from the machine's)."""
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'gitconfig'))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')


@pytest.fixture
def quick_checks(monkeypatch):
    """The fresh clone runs a stand-in for its slow checks."""
    monkeypatch.setattr(push_public, 'checks', lambda tree: ['test -f README.md && test -x run.sh'])


def test_excludes_are_literal_paths_anchored_at_the_root():
    assert push_public.read_excludes(EXCLUDES) == ['docs', 'llm', '.claude', 'AGENTS.md', 'sdk/llm']
    for bad in ('docs/', '/docs/*', '/*.md', '/a/../b', '/', '/a//b', '/./a', '/a?'):
        with pytest.raises(push_public.Refused, match='one literal path'):
            push_public.read_excludes(f'# header\n{bad}\n')


def test_exclude_handling_on_a_fake_tree(tmp_path):
    _write(tmp_path, {'docs/a.md': 'x', 'llm/b.md': 'x', 'AGENTS.md': 'x', 'sdk/llm/c.md': 'x',
                      'site/docs/d.md': 'kept', 'sdk/shared/docs/e.md': 'kept', 'sdk/docs.md': 'kept',
                      'llm_notes.md': 'kept'})
    (tmp_path / '.claude').mkdir()
    (tmp_path / '.claude/link').symlink_to('../docs')
    entries = push_public.read_excludes(EXCLUDES)
    push_public.apply_excludes(tmp_path, entries)
    for gone in ('docs', 'llm', '.claude', 'AGENTS.md', 'sdk/llm'):
        assert not os.path.lexists(tmp_path / gone), gone
    for kept in ('site/docs/d.md', 'sdk/shared/docs/e.md', 'sdk/docs.md', 'llm_notes.md'):
        assert (tmp_path / kept).is_file(), kept
    # An entry that is back is refused by name.
    (tmp_path / 'llm').mkdir()
    with pytest.raises(push_public.Refused, match='still in the staged tree: llm'):
        push_public.assert_absent(tmp_path, entries)
    assert not push_public.excluded('llm_notes.md', entries) and push_public.excluded('llm/x', entries)


def test_nested_docs_and_the_guides_must_survive(tmp_path):
    entries = push_public.read_excludes(EXCLUDES)
    tracked = ['docs/a.md', 'docs/sub/docs/b.md', 'site/docs/c.md', 'sdk/shared/docs/d.md', 'docs.md', 'x/docs']
    # A docs folder under an excluded path is not required; a file named docs is no folder.
    assert push_public.nested_docs(tracked, entries) == ['sdk/shared/docs', 'site/docs']
    _write(tmp_path, {'site/docs/c.md': 'x', 'sdk/shared/docs/d.md': 'x'})
    push_public.assert_present(tmp_path, ['sdk/shared/docs', 'site/docs'])
    shutil.rmtree(tmp_path / 'site/docs')
    with pytest.raises(push_public.Refused, match='missing: site/docs'):
        push_public.assert_present(tmp_path, ['sdk/shared/docs', 'site/docs'])
    # The guides are required even when nothing lists them.
    shutil.rmtree(tmp_path / 'sdk/shared/docs')
    with pytest.raises(push_public.Refused, match='sdk/shared/docs'):
        push_public.assert_present(tmp_path, [])


def test_staging_refuses_a_link_that_escapes_and_a_ref_that_is_no_commit(tmp_path):
    repo = _private(tmp_path)
    (repo / 'escape').symlink_to('../../outside')
    _git(repo, 'add', 'escape')
    _git(repo, 'commit', '--quiet', '-m', 'an escaping link')
    with pytest.raises(push_public.Refused, match='staging git archive'):
        push_public.stage(repo, _git(repo, 'rev-parse', 'HEAD'), tmp_path / 'stage')
    with pytest.raises(push_public.Refused, match='names no commit'):
        push_public.preflight(repo, 'no-such-ref', allow_dirty=True)


def test_symlinks_must_resolve_inside_the_tree(tmp_path):
    _write(tmp_path, {'a/file': 'x'})
    (tmp_path / 'inside').symlink_to('a/file')
    push_public.assert_links_inside(tmp_path)
    (tmp_path / 'dangling').symlink_to('docs/skill')
    with pytest.raises(push_public.Refused, match='dangling -> docs/skill'):
        push_public.assert_links_inside(tmp_path)
    (tmp_path / 'dangling').unlink()
    (tmp_path / 'outside').symlink_to(tmp_path.parent)
    with pytest.raises(push_public.Refused, match='outside'):
        push_public.assert_links_inside(tmp_path)


SECRETS = {
    'sk-': 's' + 'k-proj-' + 'Ab1' * 15,
    'csk-': 'cs' + 'k-' + 'a1b2' * 10,
    'AIza': 'AI' + 'za' + 'Sy' + 'x1' * 16 + 'Z',
    'ghp_': 'gh' + 'p_' + 'a1B2' * 9,
    'gho_': 'gh' + 'o_' + 'a1B2' * 9,
    'ghs_': 'gh' + 's_' + 'a1B2' * 9,
    'github_pat_': 'github' + '_pat_' + '11A' + 'b2C3' * 20,
    '-----BEGIN': '-----' + 'BEGIN OPENSSH PRIVATE KEY' + '-----\n' + 'b3BlbnNzaC1rZXktdjEAAAAA',
}


@pytest.mark.parametrize('shape', sorted(SECRETS))
def test_the_secret_scan_catches_each_key_shape(tmp_path, monkeypatch, shape):
    monkeypatch.setattr(shutil, 'which', lambda name: None)  # the built-in shapes alone
    _write(tmp_path, {'clean.py': 'x = 1\n', 'sub/config.json': f'{{\n  "key": "{SECRETS[shape]}"\n}}\n'})
    files, hits = push_public.scan(tmp_path)
    assert files == 2
    assert hits == [f'sub/config.json:2: {shape} shape {SECRETS[shape][:8]}…']


_HEADER = '-----' + 'BEGIN OPENSSH PRIVATE KEY' + '-----'
_BODY = 'b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB'


@pytest.mark.parametrize('text, hit', [
    (f'from {_HEADER} to the END line\n', False),           # help text naming the header
    (f'lines = ["{_HEADER}", *body]\n', False),            # a test helper building a key
    (f'{_HEADER}\n', False),                                 # a header and nothing after it
    (f'{_HEADER}\n  {_BODY}\n', False),                      # the next line is no key body
    (f'{_HEADER}\nAAAA\n', False),                           # shorter than a key body
    (f'{_HEADER}\n{_BODY}\n', True),
    (f'{_HEADER}\r\n{_BODY}\r\n', True),                     # CRLF line ends
    ('-----' + 'BEGIN RSA PRIVATE KEY' + '-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,00FF\n\n'
     f'{_BODY}\n', True),                                      # PEM headers before the body
])
def test_the_private_key_shape_needs_a_body_after_the_header(tmp_path, monkeypatch, text, hit):
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    _write(tmp_path, {'k': 'x = 1\n' + text})
    assert push_public.scan(tmp_path)[1] == (['k:2: -----BEGIN shape -----BEG…'] if hit else [])


@pytest.mark.parametrize('name, expected', [('.env', 'sub/.env: a .env file'),
                                            ('.env.local', 'sub/.env.local: a .env file'),
                                            ('node_modules/pkg/index.js', 'sub/node_modules: node_modules content')])
def test_the_secret_scan_refuses_env_files_and_node_modules(tmp_path, monkeypatch, name, expected):
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    _write(tmp_path, {f'sub/{name}': 'A=1\n'})
    assert push_public.scan(tmp_path)[1] == [expected]


def test_the_secret_scan_spares_placeholders_and_listed_published_values(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    published = 'AI' + 'za' + 'Pub' + 'L1' * 16
    monkeypatch.setattr(push_public, 'ALLOWED', {hashlib.sha256(published.encode()).hexdigest(): 'a public key'})
    body = 'f' * 64  # a content-addressed module body
    _write(tmp_path, {
        # Placeholders the repo's tests use, and a word that merely contains a prefix.
        'tests/fixtures.py': "token = 'sk-ant-oat01-private'\nmock = 'sk-collimate-conformance-mock'\n"
                             f"slug = 'task-{'x' * 40}'\nheader = '-----BEGIN-----'\n",
        f'modules/{body}': f'config={{"apiKey":"{published}"}}',
        'src/app.js': f'const key = "{published}";\n'})
    files, hits = push_public.scan(tmp_path)
    assert files == 3
    # The published value passes only inside a body named by its sha256.
    assert hits == ['src/app.js:1: AIza shape AIzaPubL…']
    assert len(push_public.ALLOWED) == 1


def test_the_secret_scan_reads_gitleaks_findings_when_it_is_on_path(tmp_path, monkeypatch):
    """gitleaks's report is filtered by the same published-value list."""
    published, leaked = 'published-value', 'leaked-value'
    monkeypatch.setattr(push_public, 'ALLOWED', {hashlib.sha256(published.encode()).hexdigest(): 'a public key'})
    report = [{'File': f'modules/{"e" * 64}', 'StartLine': 1, 'RuleID': 'gcp-api-key', 'Secret': published},
              {'File': './src/app.js', 'StartLine': 7, 'RuleID': 'generic-api-key', 'Secret': leaked}]
    fake = tmp_path / 'bin/gitleaks'
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\nwhile [ "$1" != "--report-path" ]; do shift; done\n'
                    f"printf '%s' '{json.dumps(report)}' > \"$2\"\n")
    fake.chmod(0o755)
    monkeypatch.setenv('PATH', f'{fake.parent}{os.pathsep}{os.environ["PATH"]}')
    (tmp_path / 'tree').mkdir()
    assert push_public.gitleaks(tmp_path / 'tree') == ['src/app.js:7: gitleaks generic-api-key leaked-v…']


def test_preflight_refuses_an_unpushed_ref_and_a_dirty_tree(tmp_path):
    repo = _private(tmp_path)
    head = push_public.preflight(repo, 'HEAD', allow_dirty=False)
    assert head == _git(repo, 'rev-parse', 'HEAD')
    (repo / 'README.md').write_text('edited')
    with pytest.raises(push_public.Refused, match='uncommitted changes'):
        push_public.preflight(repo, 'HEAD', allow_dirty=False)
    assert push_public.preflight(repo, 'HEAD', allow_dirty=True) == head
    _git(repo, 'commit', '--quiet', '-am', 'local only')
    with pytest.raises(push_public.Refused, match='not contained in origin/main'):
        push_public.preflight(repo, 'HEAD', allow_dirty=False)


def test_a_dry_run_pushes_nothing_and_a_push_lands_one_orphan_commit(tmp_path, capsys, quick_checks):
    repo = _private(tmp_path)
    public = _public(tmp_path)
    before = _git(public, 'rev-parse', 'main')
    argv = ['--repo', str(repo), '--public-url', str(public)]
    assert push_public.main([*argv, '--scratch', str(tmp_path / 'dry')]) == 0
    out = capsys.readouterr().out
    assert _git(public, 'for-each-ref') == f'{before} commit\trefs/heads/main'  # untouched
    assert 'dry run: nothing pushed' in out and str(public) in out
    assert 'excluded and asserted absent: /docs, /llm, /.claude, /AGENTS.md, /sdk/llm' in out
    assert 'nested docs asserted present: sdk/shared/docs, site/docs' in out
    assert 'no hits' in out and 'index.html' in out  # the diff stat against the public main
    dry = tmp_path / 'dry/public.git'
    assert {path.name for path in (tmp_path / 'dry').iterdir()} == {'public.git', 'diffstat.txt'}
    files = _git(tmp_path, '--git-dir', str(dry), 'ls-tree', '-r', '--name-only', 'publish').split('\n')
    assert sorted(files) == ['.github/sync-exclude.txt', 'README.md', 'api/pyproject.toml',
                             'build/release_policy.py', 'desktop/package.json', 'run.sh',
                             'sdk/pyproject.toml', 'sdk/shared/docs/guide.md', 'site/docs/history.md']
    assert '100755' in _git(tmp_path, '--git-dir', str(dry), 'ls-tree', 'publish', 'run.sh')

    assert push_public.main([*argv, '--push', '--scratch', str(tmp_path / 'unreviewed')]) == 1
    assert '--push needs --message-file' in capsys.readouterr().err
    assert not (tmp_path / 'unreviewed').exists()
    message = tmp_path / 'message.md'
    message.write_text('Collimate 1.2.3\n\nThe first public release.\n')
    assert push_public.main([*argv, '--push', '--message-file', str(message), '--tag', 'v1.2.3',
                             '--scratch', str(tmp_path / 'push')]) == 0
    main = _git(public, 'rev-parse', 'main')
    assert main != before and _git(public, 'rev-parse', 'v1.2.3') == main
    assert _git(public, 'log', '--format=%P|%an|%B', 'main') == '|Owner|Collimate 1.2.3\n\nThe first public release.'
    assert _git(public, 'rev-list', '--count', 'main') == '1'
    assert _git(public, 'ls-tree', '-r', '--name-only', 'main').split('\n') == sorted(files)
    capsys.readouterr()
    assert push_public.main([*argv, '--push', '--message-file', str(message), '--tag', 'v1.2.3',
                             '--scratch', str(tmp_path / 'again')]) == 1
    assert 'already has that tag' in capsys.readouterr().err
    # The lease: the dry run's commit, leased on the main it fetched, cannot
    # overwrite the main that has moved since.
    stale = push_public.Publication(dry, _git(tmp_path, '--git-dir', str(dry), 'rev-parse', 'publish'), '', before)
    with pytest.raises(push_public.Refused, match='stale info'):
        push_public.push(stale, str(public), None, None)
    assert _git(public, 'rev-parse', 'main') == main


def test_a_tag_must_name_the_staged_version(tmp_path, capsys, quick_checks):
    repo = _private(tmp_path)
    public = _public(tmp_path)
    assert push_public.main(['--repo', str(repo), '--public-url', str(public), '--tag', 'v1.2.4',
                             '--scratch', str(tmp_path / 'scratch')]) == 1
    assert "release tag 'v1.2.4' differs" in capsys.readouterr().err


def test_the_module_snapshot_branch_goes_along_when_the_tree_fetches_from_it(tmp_path, capsys, monkeypatch):
    body = b'export const mirrored = 1;'
    digest = hashlib.sha256(body).hexdigest()
    index = json.dumps({'entries': {'https://esm.sh/a@1.0.0': {'hash': digest}}})
    repo = _private(tmp_path, {'build/snapshot_store.py': '# the store', 'module-snapshot/index.json': index})
    public = _public(tmp_path)
    argv = ['--repo', str(repo), '--public-url', str(public)]
    # The fresh clone fetches the branch from its origin, as CI does.
    monkeypatch.setattr(push_public, 'checks', lambda tree: ['git fetch --depth=1 origin module-snapshot'])
    assert push_public.main([*argv, '--scratch', str(tmp_path / 'none')]) == 1
    assert 'origin has no such branch' in capsys.readouterr().err
    blob = _git(repo, 'hash-object', '-w', '--stdin', input=body.decode())
    modules = _git(repo, 'mktree', input=f'100644 blob {blob}\t{digest}\n')
    tree = _git(repo, 'mktree', input=f'040000 tree {modules}\tmodules\n')
    snapshot = _git(repo, 'commit-tree', tree, '-m', 'bodies')
    _git(repo, 'push', '--quiet', 'origin', f'{snapshot}:refs/heads/module-snapshot')
    _git(repo, 'fetch', '--quiet', 'origin')
    message = tmp_path / 'message.md'
    message.write_text('Collimate 1.2.3\n')
    assert push_public.main([*argv, '--push', '--message-file', str(message),
                             '--scratch', str(tmp_path / 'push')]) == 0
    assert f'module-snapshot {snapshot}: scanned, no hits' in capsys.readouterr().out
    assert _git(public, 'rev-parse', 'module-snapshot') == snapshot


def test_a_snapshot_branch_missing_a_body_the_index_names_is_refused(tmp_path, capsys, quick_checks):
    index = json.dumps({'entries': {'https://esm.sh/a@1.0.0': {'hash': 'a' * 64}}})
    repo = _private(tmp_path, {'build/snapshot_store.py': '# the store', 'module-snapshot/index.json': index})
    other = _git(repo, 'hash-object', '-w', '--stdin', input='another body')
    modules = _git(repo, 'mktree', input=f'100644 blob {other}\t{"b" * 64}\n')
    stale = _git(repo, 'commit-tree', _git(repo, 'mktree', input=f'040000 tree {modules}\tmodules\n'), '-m', 'old')
    _git(repo, 'push', '--quiet', 'origin', f'{stale}:refs/heads/module-snapshot')
    _git(repo, 'fetch', '--quiet', 'origin')
    assert push_public.main(['--repo', str(repo), '--public-url', str(_public(tmp_path)),
                             '--scratch', str(tmp_path / 'scratch')]) == 1
    assert 'lacks 1 of the bodies' in capsys.readouterr().err


def test_staging_this_repo_drops_the_private_paths(tmp_path):
    """The dry run's stage of this repo's HEAD: no docs/, .claude/ or
    AGENTS.md; the guides and a top-level folder's own docs/ kept; no secret."""
    staged = push_public.prepare(ROOT, _git(ROOT, 'rev-parse', 'HEAD'), tmp_path / 'stage')
    for private in ('docs', '.claude', 'AGENTS.md'):
        assert not os.path.lexists(staged.tree / private), private
        assert private in staged.entries
    assert 'sdk/shared/docs' in staged.docs and (staged.tree / 'sdk/shared/docs').is_dir()
    assert any(folder.count('/') == 1 for folder in staged.docs)
    assert (staged.tree / '.github/sync-exclude.txt').is_file()
    assert staged.files > 1000
