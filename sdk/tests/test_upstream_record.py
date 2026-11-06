"""A Docker run resolves `latest` for the harness in use and remembers the
answer, so an offline run lands on the image this machine last built."""
import json
import time

import pytest

from _env_shared import upstream  # noqa: E402

ARTIFACTS = {"codex": {"source": "npm:@openai/codex", "arg": "CODEX_VERSION"}}


@pytest.fixture
def cache(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    return tmp_path / "collimate" / "upstream-versions.json"


def test_a_resolution_is_remembered_and_reused_within_the_hour(cache, monkeypatch):
    calls = []
    monkeypatch.setattr(upstream, "latest_version", lambda source, github_token=None: calls.append(source) or "0.156.0")
    log = []
    assert upstream.resolve_requests(ARTIFACTS, {"codex": "latest"}, allow_network=True, log=log.append) == {"CODEX_VERSION": "0.156.0"}
    record = json.loads(cache.read_text())
    assert record["npm:@openai/codex"]["version_seen"] == "0.156.0"
    assert upstream.resolve_requests(ARTIFACTS, {"codex": "latest"}, allow_network=True, log=log.append) == {"CODEX_VERSION": "0.156.0"}
    assert calls == ["npm:@openai/codex"], "the second run within the hour asks no registry"
    assert any("resolved within the hour" in line for line in log)


def test_offline_falls_back_to_the_last_resolved_version_not_the_dockerfile_default(cache, monkeypatch):
    monkeypatch.setattr(upstream, "latest_version", lambda source, github_token=None: "0.156.0")
    upstream.resolve_requests(ARTIFACTS, {"codex": "latest"}, allow_network=True)
    record = json.loads(cache.read_text())
    record["npm:@openai/codex"]["checked_at"] = time.time() - 7200
    cache.write_text(json.dumps(record))

    def down(source, github_token=None):
        raise upstream.UpstreamError("registry unreachable")
    monkeypatch.setattr(upstream, "latest_version", down)
    log = []
    assert upstream.resolve_requests(ARTIFACTS, {"codex": "latest"}, allow_network=True, log=log.append) == {"CODEX_VERSION": "0.156.0"}
    assert any("using the last resolved 0.156.0" in line for line in log)
    log.clear()
    assert upstream.resolve_requests(ARTIFACTS, {"codex": "latest"}, allow_network=False, log=log.append) == {"CODEX_VERSION": "0.156.0"}
    assert any("update_packages is off" in line and "last resolved 0.156.0" in line for line in log)


def test_a_machine_that_never_resolved_keeps_the_dockerfile_default(cache, monkeypatch):
    def down(source, github_token=None):
        raise upstream.UpstreamError("offline")
    monkeypatch.setattr(upstream, "latest_version", down)
    log = []
    assert upstream.resolve_requests(ARTIFACTS, {"codex": "latest"}, allow_network=True, log=log.append) == {}
    assert any("using the pinned default" in line for line in log)
    assert upstream.resolve_requests(ARTIFACTS, {"codex": "latest"}, allow_network=False) == {}
    assert upstream.resolve_requests(ARTIFACTS, {"codex": "0.150.0"}, allow_network=False) == {"CODEX_VERSION": "0.150.0"}
    assert not cache.exists()


GITHUB_ARTIFACTS = {"code-server": {"source": "github:coder/code-server", "arg": "CODE_SERVER_VERSION"}}


class _Response:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Seen(list):
    """The Requests sent; ``state["answer"]`` answers each one."""
    state: dict


@pytest.fixture
def requests_seen(monkeypatch):
    """Every Request a lookup sends, answered by ``answer(request)`` (a
    release tag, a registry version) unless a test replaces it."""
    seen = _Seen()
    state = {"answer": lambda request: _Response({"tag_name": "v4.141.0", "version": "1.2.3",
                                                 "info": {"version": "1.2.3"}})}

    def urlopen(request, timeout=None):
        seen.append(request)
        return state["answer"](request)

    monkeypatch.setattr(upstream.urllib.request, "urlopen", urlopen)
    seen.state = state
    return seen


def _http_error(request, code):
    import urllib.error
    return urllib.error.HTTPError(request.full_url, code, "refused", hdrs=None, fp=None)


def test_github_source_sends_the_token_as_bearer(requests_seen):
    assert upstream.latest_version("github:coder/code-server", github_token="ghp_s3cret") == "4.141.0"
    [request] = requests_seen
    assert request.full_url == "https://api.github.com/repos/coder/code-server/releases/latest"
    assert request.get_header("Authorization") == "Bearer ghp_s3cret"


def test_no_token_sends_no_authorization(requests_seen):
    assert upstream.latest_version("github:coder/code-server") == "4.141.0"
    [request] = requests_seen
    assert request.get_header("Authorization") is None


def test_npm_and_pypi_never_carry_authorization(requests_seen):
    assert upstream.latest_version("npm:@openai/codex", github_token="ghp_s3cret") == "1.2.3"
    assert upstream.latest_version("pypi:uv", github_token="ghp_s3cret") == "1.2.3"
    assert len(requests_seen) == 2
    assert all(request.get_header("Authorization") is None for request in requests_seen)
    assert not any("ghp_s3cret" in json.dumps(request.header_items()) for request in requests_seen)


def test_resolve_requests_forwards_github_token(cache, monkeypatch):
    tokens = []
    monkeypatch.setattr(upstream, "latest_version",
                        lambda source, github_token=None: tokens.append(github_token) or "4.141.0")
    assert upstream.resolve_requests(GITHUB_ARTIFACTS, {"code-server": "latest"}, allow_network=True,
                                     github_token="ghp_s3cret") == {"CODE_SERVER_VERSION": "4.141.0"}
    assert tokens == ["ghp_s3cret"]


@pytest.mark.parametrize("code", [401, 403])
def test_rejected_token_retries_anonymously(requests_seen, code):
    """A token GitHub rejects (revoked, or an organisation's SSO) must not
    cost the lookup: it is asked once more, anonymously."""
    def answer(request):
        if request.get_header("Authorization"):
            raise _http_error(request, code)
        return _Response({"tag_name": "v4.141.0"})

    requests_seen.state["answer"] = answer
    assert upstream.latest_version("github:coder/code-server", github_token="ghp_s3cret") == "4.141.0"
    first, second = requests_seen
    assert first.get_header("Authorization") == "Bearer ghp_s3cret"
    assert second.get_header("Authorization") is None


def test_401_without_token_falls_back_to_last_resolution(cache, requests_seen):
    """An anonymous lookup GitHub refuses is not retried; the last version
    this machine resolved stands in."""
    upstream.remember_resolution("github:coder/code-server", "4.139.0")
    record = json.loads(cache.read_text())
    record["github:coder/code-server"]["checked_at"] = time.time() - 7200
    cache.write_text(json.dumps(record))

    def answer(request):
        raise _http_error(request, 401)

    requests_seen.state["answer"] = answer
    log = []
    assert upstream.resolve_requests(GITHUB_ARTIFACTS, {"code-server": "latest"}, allow_network=True,
                                     log=log.append) == {"CODE_SERVER_VERSION": "4.139.0"}
    assert len(requests_seen) == 1
    assert any("using the last resolved 4.139.0" in line for line in log)
