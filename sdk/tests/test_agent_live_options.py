# SPDX-License-Identifier: MIT
"""The Agent's live options (``_agent_shared.acp_run``): what a run publishes
(``gen.publish_options``) and how it applies the user's changes.

* Collimate's own options (``HarnessAdapter.live_options``) are published
  as the run starts and carried into the spawn profile; a change reopens
  each session over ``session/load`` at its next turn.
* The harness's options are its sessions' configOptions, held to the
  grant's commands level (``acp_policy.offered_options``), applied to every
  session the run has open and re-applied over every reopen.
* The session loop (``AgentSession.config_loop``) publishes at boot, polls
  the changes and republishes after each, applied or not.

Synthetic sessions throughout; the harnesses are picked from the registry
by what they declare, never by name.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import _agent_shared.acp_client as ac  # noqa: E402
import _agent_shared.acp_policy as policy  # noqa: E402
import _agent_shared.acp_run as ar  # noqa: E402
from _agent_shared.harnesses import HARNESS  # noqa: E402
from _agent_shared.session import state as session_state  # noqa: E402

ULTRA = next(name for name, row in HARNESS.items() if row.live_options.ultracode)
PLAIN = next(name for name, row in HARNESS.items() if not row.live_options.ultracode)


def _select(oid: str, current: str, values, category: str = "model") -> dict:
    return {"id": oid, "name": oid.title(), "category": category, "type": "select",
            "currentValue": current, "options": [{"value": v, "name": v.upper()} for v in values]}


@pytest.fixture(autouse=True)
def fresh_run():
    ar.clear_run_state()
    ar._ACP_SESSIONS.clear()
    yield
    ar.clear_run_state()
    ar._ACP_SESSIONS.clear()


class _Session:
    """An open session offering ``options`` and recording what it applied."""

    def __init__(self, options: list, *, applied: list | None = None, load: bool = True):
        self.options = [dict(o) for o in options]
        self.applied = applied if applied is not None else []
        self.acp_session_id = f"sid-{id(self)}"
        self.agent_caps = {"loadSession": load}
        self.loaded = False
        self.profile = ac.SpawnProfile(locus="host", cwd="/ws", spawn=None, commands="allow",
                                       fold_system_prompt=False)
        self.clock_at = time.monotonic()
        self.loop = asyncio.get_running_loop()
        self.opened_with: dict = {}
        self.on_options = None
        self.closed = False
        self.prompts: list = []

    def ui_config_options(self):
        return [dict(o) for o in self.options]

    async def apply_config_change(self, cid, value):
        self.applied.append((cid, value))
        self.options = [{**o, "currentValue": value} if o["id"] == cid else o for o in self.options]
        return self.ui_config_options()

    async def prompt(self, text, **kw):
        self.prompts.append(text)
        return "ok", [], "end_turn"

    async def close(self):
        self.closed = True


def _turn_kwargs(backend: str, **over) -> dict:
    return dict(backend=backend, prompt="go", auth="api_key", provider="anthropic", model="",
                system_prompt="", mcp_decls=[], stream=True, emit=lambda ev: None,
                commands="allow", **over)


def test_collimate_options_follow_the_harness_declaration():
    assert ar.collimate_options(PLAIN) == []
    [ultra] = ar.collimate_options(ULTRA)
    assert (ultra["id"], ultra["type"], ultra["currentValue"]) == (ar.ULTRACODE, "boolean", False)
    assert ar.profile_options(ULTRA) == {ar.ULTRACODE: False}
    ar._STICKY_CONFIG[ar.ULTRACODE] = True
    assert ar.collimate_options(ULTRA)[0]["currentValue"] is True
    assert ar.profile_options(PLAIN) == {}


def test_offered_options_hold_every_gated_option_to_the_grant(monkeypatch):
    """Model and thought-level options pass whole; every other option keeps
    only the values the harness's mode rule admits under the level (its
    current value always), and goes when nothing is left to change to."""
    monkeypatch.setattr(policy, "mode_allowed",
                        lambda backend, commands, value: value not in ("risky", True))
    options = [
        _select("model", "a", ["a", "risky"]),
        _select("effort", "low", ["low", "risky"], category="thought_level"),
        _select("mode", "safe", ["safe", "risky", "calm"], category="mode"),
        _select("agent", "safe", ["safe", "risky"], category="agent"),
        {"id": "approve_all", "name": "Approve all", "type": "boolean", "currentValue": False},
        {"id": "quiet", "name": "Quiet", "type": "boolean", "currentValue": True},
    ]
    offered = policy.offered_options("any", "deny", options)
    assert [o["id"] for o in offered] == ["model", "effort", "mode", "quiet"]
    assert [v["value"] for v in offered[0]["options"]] == ["a", "risky"]
    assert [v["value"] for v in offered[2]["options"]] == ["safe", "calm"]


def test_the_harness_options_are_shaped_and_a_misfit_is_left_out(monkeypatch):
    monkeypatch.setattr(policy, "mode_allowed", lambda backend, commands, value: True)
    async def flow():
        session = _Session([
            {**_select("model", "a", ["a", "b"]), "category": {"custom": 1}},
            {"id": "untyped", "name": "Untyped", "currentValue": "a", "options": [{"value": "a"}]},
            _select("empty", "a", []),
            {"id": "fast", "type": "boolean", "currentValue": "on"},
            {"id": "loud", "type": "boolean", "currentValue": False, "extra": 1},
        ])
        ar._absorb(PLAIN, "allow", session)
    asyncio.run(flow())
    assert ar._LAST_UI_OPTIONS == [
        {"id": "model", "name": "Model", "type": "select", "currentValue": "a",
         "options": [{"value": "a", "name": "A"}, {"value": "b", "name": "B"}]},
        {"id": "loud", "name": "loud", "type": "boolean", "currentValue": False},
    ]


def test_publishing_sends_a_changed_set_and_always_the_answer_to_a_change(native_bridge):
    ar.publish_run_options(PLAIN)
    assert native_bridge.published_options == []           # nothing to offer yet
    ar._LAST_UI_OPTIONS[:] = [_select("model", "a", ["a", "b"])]
    ar.publish_run_options(PLAIN)
    ar.publish_run_options(PLAIN)
    assert len(native_bridge.published_options) == 1
    ar.publish_run_options(PLAIN, force=True)
    assert len(native_bridge.published_options) == 2
    ar.publish_run_options(ULTRA)
    assert [o["id"] for o in native_bridge.published_options[-1]] == [ar.ULTRACODE, "model"]


def test_a_pick_goes_to_every_open_session_of_the_run_and_sticks(native_bridge):
    """Each chat thread is its own session; the run's option is all of
    theirs."""
    async def flow():
        one, two = _Session([_select("model", "a", ["a", "b"])]), _Session([_select("model", "a", ["a", "b"])])
        ar._ACP_SESSIONS[(PLAIN, "main")] = one
        ar._ACP_SESSIONS[(PLAIN, "thread-2")] = two
        ar._absorb(PLAIN, "allow", one)
        await ar.apply_config_change(PLAIN, "model", "b", commands="allow")
        return one, two
    one, two = asyncio.run(flow())
    assert one.applied == two.applied == [("model", "b")]
    assert ar._STICKY_CONFIG == {"model": "b"}
    assert ar._LAST_UI_OPTIONS[0]["currentValue"] == "b"


def test_a_pick_with_no_session_open_applies_at_the_next_open(native_bridge, monkeypatch):
    """After the idle park (or a terminal leg) no session is open, yet the
    pane still shows the options: a pick is recorded, the set shown is
    patched, and the next open applies it before its first prompt."""
    applied: list = []
    model = _select("model", "a", ["a", "b"])

    async def fake_open(**kw):
        return _Session([model], applied=applied)

    monkeypatch.setattr(ar, "_open_session", fake_open)
    monkeypatch.setattr(ar, "sync_harness_homes", lambda: None)

    async def flow():
        await ar.run_acp_turn(**_turn_kwargs(PLAIN, session_key="main"))
        await ar.close_acp_sessions()
        await ar.apply_config_change(PLAIN, "model", "b", commands="allow")
        assert applied == [] and ar._LAST_UI_OPTIONS[0]["currentValue"] == "b"
        await ar.run_acp_turn(**_turn_kwargs(PLAIN, session_key="main"))
    asyncio.run(flow())
    assert applied == [("model", "b")]
    assert native_bridge.published_options[-1][0]["currentValue"] == "b"


def test_a_pick_the_run_does_not_offer_or_the_grant_refuses_is_refused(monkeypatch):
    ar._LAST_UI_OPTIONS[:] = [_select("mode", "safe", ["safe", "risky"], category="mode"),
                              _select("model", "a", ["a", "risky"])]
    monkeypatch.setattr(ar, "mode_allowed", lambda backend, commands, value: value != "risky")
    with pytest.raises(ValueError, match="offers no option 'volume'"):
        asyncio.run(ar.apply_config_change(PLAIN, "volume", "x", commands="deny"))
    # The harness's set moved on since the host took the change: a value it
    # no longer offers is neither applied nor recorded.
    with pytest.raises(ValueError, match="Model offers no value 'max'"):
        asyncio.run(ar.apply_config_change(PLAIN, "model", "max", commands="deny"))
    with pytest.raises(PermissionError, match="beyond this runner's commands permission"):
        asyncio.run(ar.apply_config_change(PLAIN, "mode", "risky", commands="deny"))
    assert ar._STICKY_CONFIG == {}
    # A model is no way to run commands: the rule never sees it.
    asyncio.run(ar.apply_config_change(PLAIN, "model", "risky", commands="deny"))
    assert ar._STICKY_CONFIG == {"model": "risky"}
    with pytest.raises(ValueError, match="takes true or false"):
        asyncio.run(ar.apply_config_change(ULTRA, ar.ULTRACODE, "on", commands="deny"))


def test_ultracode_reopens_the_session_over_load_at_its_next_turn(native_bridge, monkeypatch):
    """A Collimate-owned pick touches no open session: the next turn closes
    it and reopens it over session/load with the pick in its profile, so
    the conversation goes on under it."""
    opened: list = []

    async def fake_open(**kw):
        opened.append({"load": kw.get("load_acp_session_id"), "live": kw.get("live")})
        session = _Session([])
        session.loaded = bool(kw.get("load_acp_session_id"))
        return session

    monkeypatch.setattr(ar, "_open_session", fake_open)
    monkeypatch.setattr(ar, "sync_harness_homes", lambda: None)

    async def flow():
        await ar.run_acp_turn(**_turn_kwargs(ULTRA, session_key="main"))
        first = ar._ACP_SESSIONS[(ULTRA, "main")]
        await ar.apply_config_change(ULTRA, ar.ULTRACODE, True, commands="allow")
        assert not first.closed and len(opened) == 1
        await ar.run_acp_turn(**_turn_kwargs(ULTRA, session_key="main"))
        second = ar._ACP_SESSIONS[(ULTRA, "main")]
        await ar.run_acp_turn(**_turn_kwargs(ULTRA, session_key="main"))
        return first, second
    first, second = asyncio.run(flow())
    assert opened == [{"load": "", "live": {ar.ULTRACODE: False}},
                      {"load": first.acp_session_id, "live": {ar.ULTRACODE: True}}]
    assert first.closed and second.opened_with == {ar.ULTRACODE: True}
    assert second.prompts == ["go", "go"]      # a loaded session: no first-turn fold
    assert [[o["currentValue"] for o in published]
            for published in native_bridge.published_options] == [[False], [True]]


def test_an_agent_reported_option_set_republishes_and_never_reaches_the_turn(native_bridge):
    """An agent that changes its own options (a mode it moved into, a model
    list that follows the model) is republished; the surface's turn stream
    never carries options."""
    session = ac.AcpSession(backend=PLAIN, profile=ac.SpawnProfile(
        locus="container", cwd="/workspace", spawn=None, commands="allow"),
        mcp_servers_for=lambda caps: ([], []))
    session.on_options = ar._options_listener(PLAIN, "allow")
    emitted: list = []
    session._sink = ac._TurnSink(emit=emitted.append, stream=True)
    session._on_update({"sessionUpdate": "config_option_update",
                        "configOptions": [_select("model", "a", ["a", "b"])]})
    session._on_update({"sessionUpdate": "current_mode_update", "currentModeId": "plan"})
    assert emitted == []
    assert [o["id"] for o in native_bridge.published_options[-1]] == ["model"]


def _agent_session(surface: str) -> session_state.AgentSession:
    return session_state.AgentSession({"backend": ULTRA, "surface": surface,
                                       "session": "interactive", "commands": "allow"})


def test_the_loop_publishes_at_boot_then_applies_each_change_and_answers_it(
        native_bridge, monkeypatch):
    applied: list = []
    said: list = []

    async def fake_apply(backend, cid, value, *, commands):
        if value == "broken":
            raise RuntimeError("the harness said no")
        applied.append((backend, cid, value, commands))

    monkeypatch.setattr(ar, "apply_config_change", fake_apply)
    monkeypatch.setattr(session_state, "OPTION_POLL_S", 0.01)
    st = _agent_session("chat")
    monkeypatch.setattr(st, "say", said.append)

    async def flow():
        ready = asyncio.Event()
        ready.set()
        loop = asyncio.ensure_future(st.config_loop(ready))
        await asyncio.sleep(0.05)
        assert len(native_bridge.published_options) == 1      # Collimate's own, at boot
        # The harness's options follow once its session opens.
        ar._LAST_UI_OPTIONS[:] = [_select("model", "a", ["a", "broken"])]
        ar.publish_run_options(ULTRA)
        native_bridge.change_option(ar.ULTRACODE, True)
        native_bridge.change_option("model", "broken")
        for _ in range(100):
            if len(native_bridge.published_options) >= 4:
                break
            await asyncio.sleep(0.01)
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
    asyncio.run(flow())
    assert applied == [(ULTRA, ar.ULTRACODE, True, "allow")]
    assert said == ["model could not be changed: the harness said no"]
    # Each change is answered by a publish, applied or not.
    assert len(native_bridge.published_options) == 4


def test_a_terminal_run_publishes_nothing_and_polls_nothing(native_bridge, monkeypatch):
    polled: list = []
    monkeypatch.setattr(session_state.gen, "pop_option_change", lambda: polled.append(1))
    st = _agent_session("terminal")

    async def flow():
        ready = asyncio.Event()
        ready.set()
        await asyncio.wait_for(st.config_loop(ready), timeout=1)
    asyncio.run(flow())
    assert native_bridge.published_options == [] and polled == []


def test_nothing_reaches_for_the_deleted_live_session_lookup():
    """``acp_run.live_session`` went with the old config pathway: callers,
    the live conformance legs included, read ``_ACP_SESSIONS`` directly."""
    import re
    from pathlib import Path
    from tests._workspace import CONTENT, SDK
    assert not hasattr(ar, "live_session")
    name = re.compile(r"\blive_session\b")
    roots = [SDK / "tests", SDK / "tests_live", SDK / "src", CONTENT / "generators"]
    offenders = [str(path) for root in roots for path in root.rglob("*.py")
                 if path != Path(__file__).resolve()
                 and name.search(path.read_text(encoding="utf-8"))]
    assert offenders == []
