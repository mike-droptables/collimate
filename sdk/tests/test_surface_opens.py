"""``opens``: the files a run serves, resolved once by
``collimate.surfaces.resolve_opens`` for the host (starting placeholders) and
the run (its surface paths), and checked by the linter (GN-03)."""
import pytest

from collimate.lint import GeneratorRef, lint_generator_manifest_data, lint_runner_manifest_data
from collimate.pointers import is_starting_pointer, starting_pointer
from collimate.surfaces import resolve_opens

FOLDER = "_runs/agent-1a2b3c4d"

FIELDS = [
    {"key": "surface", "type": "dropdown", "default": "annotation",
     "options": [{"value": "annotation"}, {"value": "chat"}, {"value": "terminal"}]},
    {"key": "session", "type": "dropdown", "default": "interactive",
     "options": ["interactive", "oneshot"], "show_if": {"surface": "annotation"}},
    {"key": "backend", "type": "dropdown", "default": "pydantic-ai",
     "options": ["pydantic-ai", "claude-code", "codex"]},
    {"key": "selected_file_first", "type": "checkbox", "default": False},
    {"key": "tab", "type": "text", "default": "preview.website"},
]

MANIFEST = {
    "id": "agent", "name": "Agent", "summary": "s", "entrypoint": "agent.py",
    "main_config": "agent-config.set_yaml",
    "ui_files": [".chat", ".json", ".terminal"],
    "requires_docker_if": {"surface": "terminal"},
    "takes_selected_file_if": {"selected_file_first": True},
    "opens": [
        {"path": "{run}/chat/conversation.chat", "if": {"surface": "chat"}},
        {"path": "{run}/terminal/{backend}.terminal", "if": {"surface": "terminal"}},
    ],
}

# A generator whose surface path ends in a template: a text field names it.
WEBSITE_FIELDS = [{"key": "tab", "type": "text", "default": "preview.website"}]
WEBSITE = {
    "id": "site", "name": "Site", "summary": "s", "entrypoint": "site.py",
    "main_config": "site.set_yaml", "ui_files": [".website"],
    "opens": [{"path": "{run}/{tab}"}],
}


def _values(**overrides):
    values = {f["key"]: f.get("default") for f in FIELDS}
    values.update(overrides)
    return values


def _site(**overrides):
    values = {"tab": "preview.website", **overrides}
    return resolve_opens(WEBSITE, WEBSITE_FIELDS, values, folder=FOLDER)


@pytest.mark.parametrize("overrides, expected", [
    ({"surface": "chat"}, [f"{FOLDER}/chat/conversation.chat"]),
    ({"surface": "terminal", "backend": "codex"}, [f"{FOLDER}/terminal/codex.terminal"]),
    ({}, []),
    ({"session": "oneshot"}, []),
])
def test_resolve_opens_evaluates_conditions_and_substitutes_GN03(overrides, expected):
    assert resolve_opens(MANIFEST, FIELDS, _values(**overrides), folder=FOLDER) == expected


@pytest.mark.parametrize("tab, expected", [
    ("preview.website", [f"{FOLDER}/preview.website"]),
    ("tabs/two.website", [f"{FOLDER}/tabs/two.website"]),
    (" tabs/two.website ", [f"{FOLDER}/tabs/two.website"]),
])
def test_resolve_opens_substitutes_a_text_field_GN03(tab, expected):
    assert _site(tab=tab) == expected


def test_resolve_opens_keeps_declaration_order_and_skips_duplicates_GN03():
    manifest = {"ui_files": [".chat", ".webview"], "opens": [
        {"path": "{run}/tab-1.webview"}, {"path": "{run}/chat/c.chat"}, {"path": "{run}/tab-1.webview"}]}
    assert resolve_opens(manifest, [], {}, folder=FOLDER) == [
        f"{FOLDER}/tab-1.webview", f"{FOLDER}/chat/c.chat"]
    assert resolve_opens({"ui_files": []}, [], {}, folder=FOLDER) == []


def test_resolve_opens_refuses_a_run_path_outside_ui_files_GN03():
    with pytest.raises(ValueError, match="ui_files"):
        _site(tab="notes.md")


@pytest.mark.parametrize("tab", ["../x.website", ".collrun/x.website", "/abs.website"])
def test_resolve_opens_refuses_unsafe_paths_GN03(tab):
    with pytest.raises(ValueError):
        _site(tab=tab)


def test_resolve_opens_refuses_a_missing_template_value_GN03():
    with pytest.raises(ValueError, match="tab"):
        _site(tab="")
    with pytest.raises(ValueError):
        resolve_opens({"opens": [{"if": {"surface": "chat"}}]}, FIELDS, _values(), folder=FOLDER)


def test_starting_pointer_shape_GN03():
    pointer = starting_pointer("Claude Code")
    assert pointer == {"starting": True, "label": "Claude Code"}
    assert is_starting_pointer(pointer)
    assert not is_starting_pointer({"chat_id": "c", "ws_path": "/ws", "token": "t", "label": "x"})
    assert not is_starting_pointer({"starting": "true"})
    assert not is_starting_pointer(None)


def _settings_yaml():
    import yaml
    return yaml.safe_dump({"fields": FIELDS})


def _lint(manifest):
    return {(i.code, i.message) for i in lint_generator_manifest_data(
        manifest, files_present={"agent.py", "agent-config.set_yaml"},
        file_contents={"agent-config.set_yaml": _settings_yaml()})}


def _codes(manifest):
    return {code for code, _ in _lint(manifest)}


def test_lint_accepts_the_agent_declaration_GN03():
    assert _lint(MANIFEST) == set()


@pytest.mark.parametrize("opens, code", [
    ("{run}/x.chat", "bad-type"),
    ([{"path": "{run}/x.chat", "when": {"surface": "chat"}}], "opens-entry"),
    ([{"if": {"surface": "chat"}}], "opens-entry"),
    ([{"path": "{run}/notes.md"}], "opens-ui-files"),
    ([{"path": "{run}/{session}"}], "opens-ui-files"),
    ([{"path": "a/{run}/x.chat"}], "opens-path"),
    ([{"path": "{nope}.website"}], "opens-path"),
    ([{"path": "{selected_file_first}.website"}], "opens-path"),
    ([{"path": "../x.website"}], "opens-path"),
    ([{"path": "_runs/other-00000000/x.chat"}], "opens-path"),
    ([{"path": "{run/x.chat"}], "opens-path"),
    ([{"path": "{run}/x.chat", "if": {"surface": "phone"}}], "manifest-condition-value"),
    ([{"path": "{run}/x.chat", "if": {"tab": "x"}}], "manifest-condition-referent-type"),
])
def test_lint_refuses_bad_opens_GN03(opens, code):
    assert code in _codes({**MANIFEST, "opens": opens})


def test_lint_checks_a_run_path_ending_in_a_template_against_its_default_GN03():
    import yaml

    def issues(default):
        fields = [{**WEBSITE_FIELDS[0], "default": default}]
        return {i.code for i in lint_generator_manifest_data(
            WEBSITE, files_present={"site.py", "site.set_yaml"},
            file_contents={"site.set_yaml": yaml.safe_dump({"fields": fields})})}

    assert issues("preview.website") == set()
    assert "opens-ui-files" in issues("notes.md")


def test_lint_requires_docker_if_GN02():
    assert "manifest-condition-redundant" in _codes({**MANIFEST, "requires_docker": True})
    assert "manifest-condition-unknown-field" in _codes(
        {**MANIFEST, "requires_docker_if": {"mode": "x"}})
    assert "bad-type" in _codes({**MANIFEST, "requires_docker_if": "terminal"})


def test_runner_docker_required_follows_requires_docker_if_GN02():
    ref = GeneratorRef.from_manifest(MANIFEST, read_file=lambda _: _settings_yaml())
    assert ref.requires_docker_if == {"surface": "terminal"}

    def codes(surface, docker):
        data = {"id": "r", "name": "R", "generator": "agent", "prompt": "",
                "settings": {"agent-config.set_yaml": _values(surface=surface)},
                "folders": [], "accounts": {}, "packages": {},
                "permissions": {"docker": docker, "internet": "full", "commands": "deny"}}
        return {i.code for i in lint_runner_manifest_data(data, resolve=lambda _: ref)}

    assert "runner-docker-required" in codes("terminal", "none")
    assert "runner-docker-required" in codes("terminal", "limited")
    assert "runner-docker-required" not in codes("terminal", "full")
    assert "runner-docker-required" not in codes("annotation", "none")
    assert "runner-docker-required" not in codes("chat", "none")
    assert ref.docker_required({"surface": "terminal"})
    assert not ref.docker_required({})  # the default surface is annotation
