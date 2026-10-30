"""Account checks explain grant-derived harness availability."""
from types import SimpleNamespace


def test_harness_diagnostics_describe_grants_and_bundled_claude__D06(monkeypatch):
    import account_adapters
    from collimate import host_harness
    monkeypatch.setattr(host_harness, "forget", lambda: None)
    monkeypatch.setattr(host_harness, "probe", lambda *_: None)
    message = account_adapters._harness_availability("codex")
    assert "docker: full uses its container" in message and "outside Snap" in message
    assert "an Annotation runner on docker: none or limited adopts docker: full" in message
    message = account_adapters._harness_availability("claude-code")
    assert "bundled" in message and "docker: none or full runs the host adapter" in message
    # A host harness is never raised from limited: none already places it.
    assert "docker: limited cannot run it" in message and "adopts" not in message
    # Its CLI is this machine's claude, which none was found for.
    assert ("its CLI runs on this machine under docker: none or full with commands ask or allow, "
            "but claude was not found on this machine") in message
    assert "its CLI runs in its container under docker: full" in account_adapters._harness_availability("codex")
    found = SimpleNamespace(blocker=None, path="/bin/codex", version="9.9.9", binary="codex")
    monkeypatch.setattr(host_harness, "probe", lambda *_: found)
    assert "docker: none uses this machine's codex 9.9.9 at /bin/codex" in account_adapters._harness_availability("codex")
    assert "docker: full uses its container" in account_adapters._harness_availability("pi")


def test_a_host_harness_says_where_its_commands_run__D06(monkeypatch):
    """A host-locus harness whose catalog row says its commands are
    sandboxed (pydantic-ai) recommends docker: full for commands: allow;
    any other host harness (claude-code) says its commands run here."""
    import account_adapters
    from collimate import host_harness
    from collimate.harness_catalog import HARNESSES
    monkeypatch.setattr(host_harness, "forget", lambda: None)
    monkeypatch.setattr(host_harness, "probe", lambda *_: None)
    assert HARNESSES["pydantic-ai"]["commands_sandboxed"] and not HARNESSES["claude-code"]["commands_sandboxed"]
    sandboxed = account_adapters._harness_availability("pydantic-ai")
    assert "docker: full sandboxes the commands its grant allows, recommended for commands: allow" in sandboxed
    assert "run on this machine" not in sandboxed
    host = account_adapters._harness_availability("claude-code")
    assert "the commands its grant allows run on this machine" in host and "sandboxes" not in host


def test_the_cli_surface_is_said_to_run_where_the_entrys_auth_puts_it(monkeypatch):
    """Verify says where an entry's CLI surface runs: Claude Code's, and
    Antigravity's on the Google plan, on this machine (with the copy found),
    Antigravity's on a Gemini key in its container."""
    import account_adapters
    from collimate import host_harness
    monkeypatch.setattr(host_harness, "forget", lambda: None)
    found = SimpleNamespace(blocker="", path="/opt/bin/agy", version="1.4.7", binary="agy")
    monkeypatch.setattr(host_harness, "probe", lambda *_: found)
    google = account_adapters._harness_availability("antigravity", "google")
    assert ("its CLI runs on this machine (agy 1.4.7 at /opt/bin/agy) under docker: none or full with "
            "commands ask or allow, outside any sandbox, signed in once for this entry") in google
    keyed = account_adapters._harness_availability("antigravity", "api_key")
    assert "its CLI runs in its container under docker: full" in keyed and "/opt/bin/agy" not in keyed
