"""Tests for collimate.lint — manifest linter."""

from pathlib import Path

import pytest

from collimate.lint import (
    GeneratorRef,
    Level,
    lint_generator_manifest,
    lint_generator_manifest_data,
    lint_path,
    lint_renderer_manifest,
    lint_renderer_manifest_data,
)

from tests._workspace import BUILTINS, GENERATORS, RENDERERS


@pytest.fixture
def tmp_manifest(tmp_path: Path):
    def make(name: str, body: str) -> Path:
        # Create entrypoint target if the body references one so checks pass by default.
        p = tmp_path / name
        p.write_text(body)
        return p

    return make


# ---------------------------------------------------------------------------
# Generator manifests
# ---------------------------------------------------------------------------


def test_generator_minimal_valid(tmp_path: Path):
    (tmp_path / "main.py").write_text("")
    (tmp_path / "cfg.set_yaml").write_text("fields: []\n")
    mf = tmp_path / "custom.generator"
    mf.write_text(
        """
id: "demo"
name: "Demo"
summary: "Demo generator."
entrypoint: "main.py"
main_config: "cfg.set_yaml"
""".strip()
    )
    issues = lint_generator_manifest(mf)
    assert not [i for i in issues if i.level == Level.ERROR]
    assert not [i for i in issues if i.level == Level.WARNING]


def test_generator_rejects_shipped_reserved_shared_cell(tmp_path: Path):
    # A builder that fabricates its own _agent_shared.py (because it didn't know
    # the real one is a helper resolved at admission) must be caught before
    # install, and told to name the helper under the cell's `shared:` instead.
    (tmp_path / "main.py").write_text("")
    (tmp_path / "_agent_shared.py").write_text("def oneshot_tag_values(*a, **k): ...")
    mf = tmp_path / "custom.generator"
    mf.write_text(
        'id: "demo"\nname: "Demo"\nsummary: "Demo."\nentrypoint: "main.py"\n'
    )
    errs = [i for i in lint_generator_manifest(mf) if i.code == "reserved-shared-cell"]
    assert errs and errs[0].level == Level.ERROR
    assert "resolved at admission from the installed helper set" in errs[0].message
    assert "`shared:`" in errs[0].message
    # Package form (_agent_shared/__init__.py) is caught too.
    (tmp_path / "_agent_shared.py").unlink()
    pkg = tmp_path / "_env_shared"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    assert [i for i in lint_generator_manifest(mf) if i.code == "reserved-shared-cell"]


def test_generator_runs_on_field(tmp_path: Path):
    (tmp_path / "main.py").write_text("")
    (tmp_path / "cfg.set_yaml").write_text("fields: []\n")
    mf = tmp_path / "custom.generator"

    def issues_for(value: str) -> list:
        mf.write_text(
            f'id: "demo"\nname: "Demo"\nsummary: "Demo."\n'
            f'entrypoint: "main.py"\nmain_config: cfg.set_yaml\nruns_on: {value}\n'
        )
        return lint_generator_manifest(mf)

    # Both declared values are clean; the field is known (no warning).
    for value in ('"anywhere"', '"local-machine"'):
        issues = issues_for(value)
        assert not [i for i in issues if i.level == Level.ERROR]
        assert not [i for i in issues if i.level == Level.WARNING]

    # Anything else is a bad enum, not a silent placement surprise.
    bad = issues_for('"the-cloud"')
    assert [(i.level, i.code) for i in bad if i.field == "runs_on"] == [
        (Level.ERROR, "bad-enum")
    ]

    non_string = issues_for("true")
    assert [(i.level, i.code) for i in non_string if i.field == "runs_on"] == [
        (Level.ERROR, "bad-type")
    ]


def test_generator_missing_required_fields(tmp_path: Path):
    mf = tmp_path / "custom.generator"
    mf.write_text("name: 'Demo'\n")
    issues = lint_generator_manifest(mf)
    codes = {i.code for i in issues}
    assert "missing-required" in codes
    missing_fields = {i.field for i in issues if i.code == "missing-required"}
    assert "id" in missing_fields
    assert "entrypoint" in missing_fields
    assert "summary" in missing_fields


def test_generator_entrypoint_file_missing(tmp_path: Path):
    mf = tmp_path / "custom.generator"
    mf.write_text(
        """
id: "demo"
name: "Demo"
summary: "x"
entrypoint: "does_not_exist.py"
""".strip()
    )
    issues = lint_generator_manifest(mf)
    codes = {(i.level, i.code) for i in issues}
    assert (Level.ERROR, "missing-file") in codes


def test_generator_unknown_field_is_warning(tmp_path: Path):
    (tmp_path / "main.py").write_text("")
    mf = tmp_path / "custom.generator"
    mf.write_text(
        """
id: "demo"
name: "Demo"
entrypoint: "main.py"
summary: "x"
surprise_field: true
""".strip()
    )
    issues = lint_generator_manifest(mf)
    unknowns = [i for i in issues if i.code == "unknown-field"]
    assert len(unknowns) == 1
    assert unknowns[0].level == Level.WARNING
    assert unknowns[0].field == "surprise_field"


def test_generator_removed_fields_are_unknown_field_warnings(tmp_path: Path):
    """Old fields removed in the cleanup sweeps should produce unknown-field
    warnings, not silent acceptance. Catches stale manifests carrying
    output / new_opt_in / default_route / summary_file / runtime /
    settings_file (the last superseded by draft_files)."""
    (tmp_path / "main.py").write_text("")
    mf = tmp_path / "custom.generator"
    mf.write_text(
        """
id: "demo"
name: "Demo"
summary: "x"
entrypoint: "main.py"
runtime: "native"
new_opt_in: true
default_route: "run"
summary_file: "x.set_yaml"
settings_file: "x.set_yaml"
output:
  default: "update"
""".strip()
    )
    issues = lint_generator_manifest(mf)
    unknown_fields = {i.field for i in issues if i.code == "unknown-field"}
    assert unknown_fields == {
        "runtime", "new_opt_in", "default_route", "summary_file",
        "settings_file", "output",
    }


def test_generator_invalid_yaml(tmp_path: Path):
    mf = tmp_path / "custom.generator"
    mf.write_text("id: [unclosed\n")
    issues = lint_generator_manifest(mf)
    assert issues
    assert issues[0].code == "yaml-parse"
    assert issues[0].level == Level.ERROR


# ---------------------------------------------------------------------------
# Renderer manifests
# ---------------------------------------------------------------------------


def test_renderer_minimal_valid(tmp_path: Path):
    (tmp_path / "main.tsx").write_text("")
    mf = tmp_path / "custom.renderer"
    mf.write_text(
        """
id: "demo"
name: "Demo"
summary: "Demo renderer."
entrypoint: "main.tsx"
embed: "native"
extensions:
  - ".txt"
""".strip()
    )
    issues = lint_renderer_manifest(mf)
    assert not [i for i in issues if i.level == Level.ERROR]
    assert not [i for i in issues if i.level == Level.WARNING]


def test_renderer_bad_embed_mode(tmp_path: Path):
    (tmp_path / "main.tsx").write_text("")
    mf = tmp_path / "custom.renderer"
    mf.write_text(
        """
id: "demo"
name: "Demo"
summary: "x"
entrypoint: "main.tsx"
embed: "webcomponent"
""".strip()
    )
    issues = lint_renderer_manifest(mf)
    assert any(i.code == "bad-enum" and i.field == "embed" for i in issues)


def test_renderer_extension_missing_dot_is_error(tmp_path: Path):
    """Dot-prefix was promoted from warning → error in the cleanup sweep."""
    (tmp_path / "main.tsx").write_text("")
    mf = tmp_path / "custom.renderer"
    mf.write_text(
        """
id: "demo"
name: "Demo"
summary: "x"
entrypoint: "main.tsx"
extensions:
  - "md"
""".strip()
    )
    issues = lint_renderer_manifest(mf)
    errs = [i for i in issues if i.code == "ext-no-dot"]
    assert len(errs) == 1
    assert errs[0].level == Level.ERROR


def test_renderer_supports_diff_wrong_type(tmp_path: Path):
    (tmp_path / "main.tsx").write_text("")
    mf = tmp_path / "custom.renderer"
    mf.write_text(
        """
id: "demo"
name: "Demo"
summary: "x"
entrypoint: "main.tsx"
supports_diff: "yes"
""".strip()
    )
    issues = lint_renderer_manifest(mf)
    assert any(i.code == "bad-type" and i.field == "supports_diff" for i in issues)


def test_renderer_summary_required(tmp_path: Path):
    (tmp_path / "main.tsx").write_text("")
    mf = tmp_path / "custom.renderer"
    mf.write_text(
        """
id: "demo"
name: "Demo"
entrypoint: "main.tsx"
""".strip()
    )
    issues = lint_renderer_manifest(mf)
    assert any(
        i.level == Level.ERROR and i.code == "missing-required" and i.field == "summary"
        for i in issues
    )


# ---------------------------------------------------------------------------
# lint_path auto-detection and directory walks
# ---------------------------------------------------------------------------


def test_lint_path_auto_detects_renderer(tmp_path: Path):
    (tmp_path / "main.tsx").write_text("")
    mf = tmp_path / "custom.renderer"
    mf.write_text("id: 'x'\nname: 'x'\nsummary: 'x'\nentrypoint: 'main.tsx'\n")
    issues = lint_path(mf)
    assert not [i for i in issues if i.level == Level.ERROR]


def test_lint_path_walks_directory(tmp_path: Path):
    # Two manifests: one valid, one broken.
    good = tmp_path / "good"
    good.mkdir()
    (good / "main.py").write_text("")
    (good / "custom.generator").write_text(
        "id: 'a'\nname: 'A'\nentrypoint: 'main.py'\nsummary: 'ok'\n"
    )
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "custom.generator").write_text("name: 'B'\n")  # missing id + entrypoint

    issues = lint_path(tmp_path)
    by_code = {(i.path.parent.name, i.code) for i in issues}
    assert ("bad", "missing-required") in by_code


def test_lint_path_unknown_file(tmp_path: Path):
    f = tmp_path / "random.yaml"
    f.write_text("id: 'x'\n")
    issues = lint_path(f)
    assert len(issues) == 1
    assert issues[0].code == "unknown-kind"


# ---------------------------------------------------------------------------
# In-memory variants — used by the generator-builders to validate the
# agent's stage before commit (no filesystem write).
# ---------------------------------------------------------------------------


_BASE_GEN: dict = {
    "id": "demo",
    "name": "Demo",
    "summary": "Demo.",
    "entrypoint": "main.py",
    "main_config": "cfg.set_yaml",
}


def test_generator_data_minimal_valid():
    issues = lint_generator_manifest_data(
        _BASE_GEN,
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert not [i for i in issues if i.level == Level.ERROR]


def test_generator_data_entrypoint_not_in_stage():
    """The exact bug class the linter exists to catch: agent declared an
    entrypoint it forgot to write."""
    issues = lint_generator_manifest_data(
        _BASE_GEN,
        files_present={"other.py"},
    )
    assert any(
        i.level == Level.ERROR and i.code == "missing-file" and i.field == "entrypoint"
        for i in issues
    ), [i.format() for i in issues]


def test_generator_data_category_is_now_unknown_field():
    """``category`` was removed in the modify/create rework — generators are
    grouped by their source cell's name now. A leftover ``category`` is an
    unknown-field warning (not an error)."""
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "category": "weird"},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(i.code == "unknown-field" and i.field == "category" for i in issues)
    assert not any(i.level == Level.ERROR for i in issues)


# ── retired input-type fields ─────────────────────────────────────────


def test_generator_data_retired_input_type_fields_are_unknown_fields():
    """The input-type system is gone: the ``input`` shape field and the older
    ``type`` array / ``inputs`` block now surface as unknown-field warnings,
    not errors and not silent acceptance."""
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "input": "stack", "type": ["create"],
         "inputs": {"cells": {"min": 0, "max": 1}}},
        files_present={"main.py", "cfg.set_yaml"},
    )
    unknown_fields = {i.field for i in issues if i.code == "unknown-field"}
    assert "input" in unknown_fields
    assert "type" in unknown_fields
    assert "inputs" in unknown_fields
    assert not any(i.level == Level.ERROR for i in issues)


# ── summary required + length warning ────────────────────────────────


def test_generator_data_summary_required():
    manifest = {**_BASE_GEN}
    manifest.pop("summary")
    issues = lint_generator_manifest_data(
        manifest,
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(
        i.level == Level.ERROR and i.code == "missing-required" and i.field == "summary"
        for i in issues
    )


def test_generator_data_summary_empty_string_errors():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "summary": "   "},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(
        i.level == Level.ERROR and i.field == "summary" for i in issues
    )


def test_generator_data_summary_too_long_warns():
    """3+ sentences trips the soft-warning heuristic."""
    long_summary = "First sentence. Second sentence. Third sentence."
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "summary": long_summary},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(
        i.level == Level.WARNING and i.code == "summary-too-long"
        for i in issues
    )


def test_generator_data_summary_one_sentence_no_warning():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "summary": "Does the thing."},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert not any(i.code == "summary-too-long" for i in issues)


def test_generator_data_summary_two_sentences_no_warning():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "summary": "Does the thing. Returns a cell."},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert not any(i.code == "summary-too-long" for i in issues)


# ── icon ─────────────────────────────────────────────────────────────


def test_generator_data_icon_is_now_unknown_field():
    """``icon`` is not a generator manifest field: a leftover ``icon`` is an
    unknown-field warning, not an error."""
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "icon": "sparkles"},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(i.code == "unknown-field" and i.field == "icon" for i in issues)
    assert not [i for i in issues if i.level == Level.ERROR]


# ── ephemeral (removed) ──────────────────────────────────────────────


def test_generator_data_ephemeral_is_now_unknown_field():
    """The ephemeral-generator concept is gone: a live session is an ordinary
    run stopped with the Stop button. A leftover ``ephemeral`` is an
    unknown-field warning, not an error."""
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "ephemeral": True},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(i.code == "unknown-field" and i.field == "ephemeral" for i in issues)
    assert not [i for i in issues if i.level == Level.ERROR]


# ── parameters (not a field) ─────────────────────────────────────────


def test_generator_data_parameters_is_an_unknown_field():
    """A generator takes no run parameters — its runner's settings are its
    whole configuration — so a ``parameters`` block is an unknown-field
    warning, not an error."""
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "parameters": {"mode": {"options": ["one", "two"], "default": "one"}}},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(i.code == "unknown-field" and i.field == "parameters" for i in issues)
    assert not [i for i in issues if i.level == Level.ERROR]


# ── timeout_seconds validator ────────────────────────────────────────


def test_generator_data_timeout_seconds_valid():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "timeout_seconds": 600},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert not [i for i in issues if i.field == "timeout_seconds"]


@pytest.mark.parametrize("bad", [0, -5, "x", True])
def test_generator_data_timeout_seconds_bad(bad):
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "timeout_seconds": bad},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(
        i.level == Level.ERROR and i.code == "bad-type" and i.field == "timeout_seconds"
        for i in issues
    )


# ── rlimits validator ────────────────────────────────────────────────


def test_generator_data_rlimits_valid():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "rlimits": {"memory_bytes": 8 * 1024**3, "cpu_seconds": 600}},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert not [i for i in issues if i.level == Level.ERROR]


def test_generator_data_rlimits_unknown_key_errors():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "rlimits": {"memory_gb": 8}},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(
        i.level == Level.ERROR and i.code == "bad-enum" and i.field == "rlimits.memory_gb"
        for i in issues
    )


def test_generator_data_rlimits_non_positive_errors():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "rlimits": {"memory_bytes": 0}},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(
        i.level == Level.ERROR and i.code == "bad-type" and i.field == "rlimits.memory_bytes"
        for i in issues
    )

    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "rlimits": {"memory_bytes": "lots"}},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(
        i.level == Level.ERROR and i.code == "bad-type" and i.field == "rlimits.memory_bytes"
        for i in issues
    )


def test_generator_data_rlimits_must_be_mapping():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "rlimits": [1, 2, 3]},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(i.code == "bad-type" and i.field == "rlimits" for i in issues)


# ── env_passthrough validator ────────────────────────────────────────


def test_generator_data_env_passthrough_valid():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "env_passthrough": ["SSH_AUTH_SOCK"]},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert not [i for i in issues if i.level == Level.ERROR]


def test_generator_data_env_passthrough_must_be_list():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "env_passthrough": "SSH_AUTH_SOCK"},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(i.code == "bad-type" and i.field == "env_passthrough" for i in issues)


def test_generator_data_env_passthrough_empty_string_errors():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "env_passthrough": [""]},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(
        i.level == Level.ERROR and i.code == "bad-type" and i.field == "env_passthrough"
        for i in issues
    )


def test_generator_data_env_passthrough_secret_looking_warns():
    """Names matching KEY/TOKEN/SECRET warn — route those through gen.secret."""
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "env_passthrough": ["MY_API_TOKEN"]},
        files_present={"main.py", "cfg.set_yaml"},
    )
    warns = [
        i for i in issues
        if i.code == "env-passthrough-looks-secret"
    ]
    assert warns and warns[0].level == Level.WARNING


# ── draft files ──────────────────────────────────────────────────────


def test_generator_data_draft_files_warn_when_missing():
    issues = lint_generator_manifest_data(
        {
            **_BASE_GEN,
            "draft_files": ["pydantic-config.set_yaml"],
        },
        files_present={"main.py", "cfg.set_yaml"},
    )
    warns = [i for i in issues if i.code == "missing-file" and i.field == "draft_files"]
    assert warns and warns[0].level == Level.WARNING


# ── prompt_required ──────────────────────────────────────────────────


_INTENT_GEN: dict = {
    **_BASE_GEN,
    "prompt_intent": "What to do",
}


def test_generator_data_prompt_required_needs_intent():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "prompt_required": True},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(
        i.level == Level.ERROR and i.code == "prompt-required-without-intent"
        for i in issues
    )

    # With a prompt_intent it's legal.
    issues = lint_generator_manifest_data(
        {**_INTENT_GEN, "prompt_required": True},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert not [i for i in issues if i.level == Level.ERROR]


def test_generator_data_prompt_required_must_be_bool():
    issues = lint_generator_manifest_data(
        {**_INTENT_GEN, "prompt_required": "yes"},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(
        i.code == "bad-type" and i.field == "prompt_required" for i in issues
    )


# ── prompt_show_if / prompt_required_if ──────────────────────────────


_MAIN_CFG = "main.set_yaml"
_PROMPT_COND_GEN: dict = {
    **_INTENT_GEN,
    "main_config": _MAIN_CFG,
}
_MODE_SET_YAML = """
fields:
  - key: mode
    type: dropdown
    label: Mode
    options: [once, open]
    default: once
""".strip()


def _prompt_cond_issues(manifest: dict) -> list:
    return lint_generator_manifest_data(
        manifest,
        files_present={"main.py", _MAIN_CFG},
        file_contents={_MAIN_CFG: _MODE_SET_YAML},
    )


def test_generator_data_prompt_conditions_are_valid_over_the_main_config():
    """Both conditional forms are legal when they name a main-config field —
    including through the v2 combinators."""
    issues = _prompt_cond_issues({
        **_PROMPT_COND_GEN,
        "prompt_show_if": {"mode": "once"},
        "prompt_required_if": {"any": [{"mode": "once"}, {"mode": "open"}]},
    })
    assert not [i for i in issues if i.level == Level.ERROR]


def test_generator_data_prompt_conditions_need_an_intent():
    """A condition gates the box ``prompt_intent`` declares — without one it
    has nothing to gate."""
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "main_config": _MAIN_CFG,
         "prompt_show_if": {"mode": "once"}},
        files_present={"main.py", _MAIN_CFG},
        file_contents={_MAIN_CFG: _MODE_SET_YAML},
    )
    assert any(
        i.level == Level.ERROR and i.code == "prompt-condition-without-intent"
        and i.field == "prompt_show_if"
        for i in issues
    )


def test_generator_data_prompt_conditions_must_name_a_main_config_field():
    issues = _prompt_cond_issues({
        **_PROMPT_COND_GEN,
        "prompt_required_if": {"nonesuch": "once"},
    })
    assert any(
        i.level == Level.ERROR and i.code == "prompt-condition-unknown-field"
        and i.field == "prompt_required_if"
        for i in issues
    )


def test_generator_data_prompt_conditions_must_be_non_empty_maps():
    """Including the bare `prompt_show_if:` YAML writes as ``None`` — the app
    refuses that manifest, so the linter has to name it too."""
    for bad in ({}, "once", [], None):
        issues = _prompt_cond_issues({**_PROMPT_COND_GEN, "prompt_show_if": bad})
        assert any(
            i.code == "bad-type" and i.field == "prompt_show_if" for i in issues
        ), bad


def test_generator_data_prompt_conditions_check_combinator_structure():
    """A malformed combinator evaluates to False at runtime (the prompt box
    never shows), so it gets the same structure errors as a field show_if —
    even when the main config can't be read (structure isn't content-gated)."""
    combo_codes = {"cond-combinator-arg", "cond-combinator-mixed"}
    bad_shapes = (
        {"any": "mode"},
        {"any": []},
        {"any": [{"mode": "once"}], "mode": "once"},
        {"all": []},
        {"not": "mode"},
        {"any": ["mode"]},
    )
    for bad in bad_shapes:
        manifest = {**_PROMPT_COND_GEN, "prompt_show_if": bad}
        for issues in (
            _prompt_cond_issues(manifest),
            lint_generator_manifest_data(
                manifest, files_present={"main.py", _MAIN_CFG}),
        ):
            assert any(
                i.level == Level.ERROR and i.code in combo_codes
                and i.field == "prompt_show_if"
                for i in issues
            ), bad
    for good in ({"not": {"mode": "once"}}, {"any": [{"mode": "once"}]}):
        issues = _prompt_cond_issues({**_PROMPT_COND_GEN, "prompt_show_if": good})
        assert not [i for i in issues if i.code in combo_codes], good


# ── config_folders ───────────────────────────────────────────────────
#
# A generator declares the config-folder slots its saved runners may fill.


def test_generator_data_config_folders_is_known_field():
    """config_folders is a first-class field — no unknown-field warning, and a
    well-formed block (name + description) lints clean with no sample."""
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "config_folders": [
            {"name": "_skills", "description": "Agent skills."},
        ]},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert not [i for i in issues if i.level == Level.ERROR]
    assert not any(i.field == "config_folders" and i.code == "unknown-field"
                   for i in issues)


def test_generator_data_config_folders_sample_under_samples_ok():
    """A sample that exists and lives under samples/ is valid (a directory
    sample is fine — file_exists just checks the path is present)."""
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "config_folders": [
            {"name": "_skills", "description": "x", "sample": "samples/_skills"},
        ]},
        files_present={"main.py", "cfg.set_yaml", "samples/_skills"},
    )
    assert not [i for i in issues if i.level == Level.ERROR]


def test_generator_data_config_folders_must_be_list():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "config_folders": {"name": "_skills"}},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(i.code == "bad-type" and i.field == "config_folders" for i in issues)


def test_generator_data_config_folders_bad_name():
    for bad in ("has space", "bad/slash", "x" * 65):
        issues = lint_generator_manifest_data(
            {**_BASE_GEN, "config_folders": [{"name": bad, "description": "x"}]},
            files_present={"main.py", "cfg.set_yaml"},
        )
        assert any(
            i.level == Level.ERROR and i.code == "bad-config-folder-name"
            for i in issues
        ), f"name {bad!r} should be rejected"

    # A leading underscore / dots / hyphens are fine.
    for good in ("_skills", "_mcp", "build", "scripts", "a.b-c_d"):
        issues = lint_generator_manifest_data(
            {**_BASE_GEN, "config_folders": [{"name": good, "description": "x"}]},
            files_present={"main.py", "cfg.set_yaml"},
        )
        assert not any(i.code == "bad-config-folder-name" for i in issues), \
            f"name {good!r} should be accepted"


def test_generator_data_config_folders_reserved_name():
    for bad in (".collrun", ".", "..sneaky"):
        issues = lint_generator_manifest_data(
            {**_BASE_GEN, "config_folders": [{"name": bad, "description": "x"}]},
            files_present={"main.py", "cfg.set_yaml"},
        )
        assert any(
            i.level == Level.ERROR and i.code == "reserved-config-folder-name"
            for i in issues
        ), f"name {bad!r} should be reserved-rejected"


def test_generator_data_config_folders_duplicate_name():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "config_folders": [
            {"name": "_skills", "description": "a"},
            {"name": "_skills", "description": "b"},
        ]},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(
        i.level == Level.ERROR and i.code == "duplicate-config-folder"
        for i in issues
    )


def test_generator_data_config_folders_missing_description():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "config_folders": [{"name": "_skills"}]},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert any(
        i.level == Level.ERROR and i.code == "missing-required"
        and i.field == "config_folders"
        for i in issues
    )


def test_generator_data_config_folders_sample_not_under_samples():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "config_folders": [
            {"name": "_skills", "description": "x", "sample": "starters/_skills"},
        ]},
        files_present={"main.py", "starters/_skills"},
    )
    assert any(
        i.level == Level.ERROR and i.code == "sample-not-under-samples"
        for i in issues
    )


def test_generator_data_config_folders_sample_missing_file():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "config_folders": [
            {"name": "_skills", "description": "x", "sample": "samples/_skills"},
        ]},
        files_present={"main.py", "cfg.set_yaml"},  # sample path absent
    )
    assert any(
        i.level == Level.ERROR and i.code == "missing-file"
        and i.field == "config_folders"
        for i in issues
    )


def test_generator_data_config_folders_unknown_entry_key_warns():
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "config_folders": [
            {"name": "_skills", "description": "x", "icon": "sparkles"},
        ]},
        files_present={"main.py", "cfg.set_yaml"},
    )
    warns = [i for i in issues if i.code == "unknown-field"
             and i.field == "config_folders[0].icon"]
    assert warns and warns[0].level == Level.WARNING
    assert not [i for i in issues if i.level == Level.ERROR]


# ── required-on-checkbox (standalone .set_yaml field check) ───────────
#
# The .set_yaml condition pass checks required fields on checkbox controls.


def test_generator_data_required_on_checkbox_warns():
    set_yaml = """
fields:
  - key: enabled
    type: checkbox
    label: Enabled
    default: true
    required: true
""".strip()
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "draft_files": ["cfg.set_yaml"]},
        files_present={"main.py", "cfg.set_yaml"},
        file_contents={"cfg.set_yaml": set_yaml},
    )
    warns = [i for i in issues if i.code == "required-on-checkbox"]
    assert warns and warns[0].level == Level.WARNING

    # required on a non-checkbox field is the normal run gate — no warn.
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "draft_files": ["cfg.set_yaml"]},
        files_present={"main.py", "cfg.set_yaml"},
        file_contents={
            "cfg.set_yaml": "fields:\n  - {key: m, type: text, required: true}\n"
        },
    )
    assert not any(i.code == "required-on-checkbox" for i in issues)


def test_required_on_checkbox_file_based_reads_set_yaml_from_disk(tmp_path: Path):
    """lint_generator_manifest wires read_file from the manifest's parent
    dir, so the content-gated .set_yaml field check fires on disk too."""
    (tmp_path / "main.py").write_text("")
    (tmp_path / "cfg.set_yaml").write_text(
        "fields:\n  - {key: enabled, type: checkbox, default: true, required: true}\n"
    )
    mf = tmp_path / "custom.generator"
    mf.write_text(
        """
id: "demo"
name: "Demo"
summary: "x"
entrypoint: "main.py"
draft_files:
  - "cfg.set_yaml"
""".strip()
    )
    issues = lint_generator_manifest(mf)
    assert any(i.code == "required-on-checkbox" for i in issues)


# ── renderer in-memory variants ──────────────────────────────────────


def test_renderer_data_minimal_valid():
    issues = lint_renderer_manifest_data(
        {"id": "demo", "name": "Demo", "summary": "x", "entrypoint": "main.tsx"},
        files_present={"main.tsx"},
    )
    assert not [i for i in issues if i.level == Level.ERROR]


def test_renderer_data_entrypoint_not_in_stage():
    issues = lint_renderer_manifest_data(
        {"id": "demo", "name": "Demo", "summary": "x", "entrypoint": "main.tsx"},
        files_present={"other.tsx"},
    )
    assert any(
        i.level == Level.ERROR and i.code == "missing-file" and i.field == "entrypoint"
        for i in issues
    )


def test_renderer_data_summary_required():
    issues = lint_renderer_manifest_data(
        {"id": "demo", "name": "Demo", "entrypoint": "main.tsx"},
        files_present={"main.tsx"},
    )
    assert any(
        i.level == Level.ERROR and i.code == "missing-required" and i.field == "summary"
        for i in issues
    )


def _default_for_errors(default_for, extensions=(".a", ".B")):
    data = {"id": "demo", "name": "Demo", "summary": "x", "entrypoint": "main.tsx",
            "extensions": list(extensions), "default_for": default_for}
    return [(i.code, i.message) for i in lint_renderer_manifest_data(data, files_present={"main.tsx"})
            if i.level == Level.ERROR]


def test_renderer_default_for_names_claimed_extensions_or_any_text():
    # An extension it claims (case-insensitively, as the frontend matches) or
    # '*' for every text file.
    assert _default_for_errors([".a", ".b", "*"]) == []
    assert _default_for_errors(["*"], extensions=()) == []
    assert _default_for_errors([]) == []


@pytest.mark.parametrize("default_for, code, fragment", [
    (".a", "bad-type", "'default_for' must be a list"),
    ([1], "bad-type", "'default_for[0]' must be a string"),
    ([".c"], "default-for-unclaimed", "default_for '.c' is not one of this renderer's extensions"),
    (["a"], "default-for-unclaimed", "default_for 'a' is not one of this renderer's extensions"),
    (["**"], "default-for-unclaimed", "default_for '**'"),
    ([".a", ".A"], "default-for-duplicate", "default_for lists '.A' twice"),
])
def test_renderer_default_for_refuses_what_it_cannot_be_the_default_for(default_for, code, fragment):
    errors = _default_for_errors(default_for)
    assert [c for c, _m in errors] == [code] and fragment in errors[0][1], errors


def test_generator_data_bad_root():
    issues = lint_generator_manifest_data(
        ["not", "a", "dict"],  # type: ignore[arg-type]
        files_present=set(),
    )
    assert any(i.code == "bad-root" for i in issues)


# ---------------------------------------------------------------------------
# Regression: bundled manifests in the SDK tree lint cleanly.
# ---------------------------------------------------------------------------


def test_bundled_generators_lint_clean():
    """Every manifest shipped in the workspace must lint without errors."""
    issues = []
    for d in (GENERATORS, RENDERERS, BUILTINS):
        issues.extend(lint_path(d))
    errors = [i for i in issues if i.level == Level.ERROR]
    assert not errors, "bundled manifests have lint errors:\n" + "\n".join(
        i.format() for i in errors
    )


# ---------------------------------------------------------------------------
# Doc examples: every ```yaml manifest snippet in the authoring guides must
# lint clean against the current schema. Catches schema drift between code
# and docs (the LLM uses the docs at runtime via gen_rend_builder).
# ---------------------------------------------------------------------------


def _extract_manifest_blocks(markdown_path: Path) -> list[dict]:
    """Return every ```yaml block in `markdown_path` that looks like a
    generator manifest (has id + entrypoint), parsed to a dict."""
    import re
    text = markdown_path.read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", text, flags=re.DOTALL)
    out: list[dict] = []
    for b in blocks:
        try:
            data = yaml.safe_load(b)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and "id" in data and "entrypoint" in data:
            out.append(data)
    return out


def test_generator_doc_manifests_lint_clean():
    """Every manifest snippet in the generator authoring docs must lint
    cleanly under the current schema. If the schema changes, doc
    examples must move with it — otherwise gen_rend_builder ships a
    stale prompt to the LLM."""
    doc = Path(__file__).resolve().parents[1] / "shared" / "docs" / "how-to-write-a-generator.md"
    failures: list[str] = []
    for i, manifest in enumerate(_extract_manifest_blocks(doc)):
        # Synthesize the files `entrypoint` / `draft_files` /
        # `config_folders[].sample` reference so the file-existence checks
        # pass; we're only validating the manifest contract, not actual
        # files.
        files = {manifest.get("entrypoint", "main.py")}
        for f in manifest.get("draft_files") or []:
            if isinstance(f, str):
                files.add(f)
        for entry in manifest.get("config_folders") or []:
            if isinstance(entry, dict) and isinstance(entry.get("sample"), str):
                files.add(entry["sample"])
        for entry in manifest.get("configs") or []:
            files.add(entry["path"])
        issues = lint_generator_manifest_data(
            manifest,
            files_present=files,
        )
        errors = [iss for iss in issues if iss.level == Level.ERROR]
        if errors:
            failures.append(
                f"{doc.name} block #{i} (id={manifest.get('id')!r}):\n  "
                + "\n  ".join(e.format() for e in errors)
            )
    assert not failures, "doc manifest examples have lint errors:\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# Generator manifest declarations
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Conditional-field lint: show_if / required_if / enable_if
# (_check_set_yaml_conditions). One positive + one negative per rule code.
# The pass is content-gated like the other .set_yaml field checks, so
# every case ships the .set_yaml body via file_contents.
# ---------------------------------------------------------------------------


# Every code the conditional-field pass can emit — used by the regression
# test to prove a no-conditions file adds nothing.
_CONDITION_CODES = {
    "cond-unknown-field", "cond-forward-reference", "cond-self-reference",
    "cond-cycle", "cond-value-not-an-option", "cond-referent-type",
    "cond-value-type-mismatch", "cond-reserved-key", "required-if-on-checkbox",
    "required-with-show_if", "required-if-permanently-hidden", "show-when-alias",
    "required-if-reference-superset", "cond-empty", "enable-if-unknown-field",
    "enable-if-referent-type", "enable-if-value-not-an-option",
    "enable-if-default-disabled", "enable-if-empty", "enable-if-on-non-dropdown",
    "enable-if-reserved-key", "enable-if-reference-superset",
}


def _lint_set_yaml(body: str):
    """Lint a generator whose single ``cfg.set_yaml`` draft holds ``body``.

    Content-gated: file_contents carries the draft so the condition pass
    actually runs.
    """
    return lint_generator_manifest_data(
        {
            **_BASE_GEN,
            "draft_files": ["cfg.set_yaml"],
        },
        files_present={"main.py", "cfg.set_yaml"},
        file_contents={"cfg.set_yaml": body},
    )


def _has(issues, code, level=None):
    return any(
        i.code == code and (level is None or i.level == level) for i in issues
    )


# ── regression + alias folding ───────────────────────────────────────


def test_conditions_no_conditions_file_adds_nothing():
    """A .set_yaml with no show_if/required_if/enable_if produces zero
    condition issues (backward-compat: unconditioned files behave as today)."""
    body = """
fields:
  - {key: alpha, type: text, default: "a"}
  - {key: beta, type: number, default: 1}
  - {key: mode, type: dropdown, options: [chat, oneshot], default: chat}
  - {key: flag, type: checkbox, default: true}
""".strip()
    issues = _lint_set_yaml(body)
    assert not ({i.code for i in issues} & _CONDITION_CODES)
    assert not [i for i in issues if i.level == Level.ERROR]


def test_conditions_skipped_without_file_contents():
    """No file_contents ⇒ content-gated pass skips silently (no false
    positives from an unreadable draft)."""
    issues = lint_generator_manifest_data(
        {**_BASE_GEN, "draft_files": ["cfg.set_yaml"]},
        files_present={"main.py", "cfg.set_yaml"},
    )
    assert not ({i.code for i in issues} & _CONDITION_CODES)


# ── rule 1: cond-unknown-field ───────────────────────────────────────


def test_cond_unknown_field():
    fires = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [chat, oneshot]}
  - {key: hist, type: number, default: 1, show_if: {nope: chat}}
""".strip())
    assert _has(fires, "cond-unknown-field", Level.ERROR)

    clean = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [chat, oneshot]}
  - {key: hist, type: number, default: 1, show_if: {mode: chat}}
""".strip())
    assert not _has(clean, "cond-unknown-field")


# ── rule 2: cond-forward-reference ───────────────────────────────────


def test_cond_forward_reference():
    fires = _lint_set_yaml("""
fields:
  - {key: hist, type: number, default: 1, show_if: {mode: chat}}
  - {key: mode, type: dropdown, options: [chat, oneshot]}
""".strip())
    assert _has(fires, "cond-forward-reference", Level.WARNING)

    clean = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [chat, oneshot]}
  - {key: hist, type: number, default: 1, show_if: {mode: chat}}
""".strip())
    assert not _has(clean, "cond-forward-reference")


# ── rule 3: cond-self-reference ──────────────────────────────────────


def test_cond_self_reference():
    fires = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [chat], show_if: {mode: chat}}
""".strip())
    assert _has(fires, "cond-self-reference", Level.ERROR)

    clean = _lint_set_yaml("""
fields:
  - {key: a, type: dropdown, options: [x]}
  - {key: mode, type: dropdown, options: [chat], show_if: {a: x}}
""".strip())
    assert not _has(clean, "cond-self-reference")


# ── rule 4: cond-cycle ───────────────────────────────────────────────


def test_cond_cycle():
    fires = _lint_set_yaml("""
fields:
  - {key: a, type: dropdown, options: [x], show_if: {b: x}}
  - {key: b, type: dropdown, options: [x], show_if: {a: x}}
""".strip())
    assert _has(fires, "cond-cycle", Level.WARNING)

    clean = _lint_set_yaml("""
fields:
  - {key: a, type: dropdown, options: [x]}
  - {key: b, type: dropdown, options: [x], show_if: {a: x}}
""".strip())
    assert not _has(clean, "cond-cycle")

    # A cycle routed through a combinator is still a cycle.
    via_any = _lint_set_yaml("""
fields:
  - {key: a, type: dropdown, options: [x], show_if: {any: [{b: x}]}}
  - {key: b, type: dropdown, options: [x], show_if: {a: x}}
""".strip())
    assert _has(via_any, "cond-cycle", Level.WARNING)

    via_not = _lint_set_yaml("""
fields:
  - {key: a, type: dropdown, options: [x], required_if: {not: {b: x}}}
  - {key: b, type: dropdown, options: [x], show_if: {a: x}}
""".strip())
    assert _has(via_not, "cond-cycle", Level.WARNING)


# ── rule 5: cond-value-not-an-option ─────────────────────────────────


def test_cond_value_not_an_option():
    fires = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [chat, oneshot]}
  - {key: hist, type: number, default: 1, show_if: {mode: typo}}
""".strip())
    assert _has(fires, "cond-value-not-an-option", Level.ERROR)

    clean = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [chat, oneshot]}
  - {key: hist, type: number, default: 1, show_if: {mode: chat}}
""".strip())
    assert not _has(clean, "cond-value-not-an-option")


# ── rule 5b: cond-referent-type ──────────────────────────────────────


def test_cond_referent_type():
    fires = _lint_set_yaml("""
fields:
  - {key: name, type: text}
  - {key: hist, type: number, default: 1, show_if: {name: bob}}
""".strip())
    assert _has(fires, "cond-referent-type", Level.ERROR)

    clean = _lint_set_yaml("""
fields:
  - {key: name, type: dropdown, options: [bob, alice]}
  - {key: hist, type: number, default: 1, show_if: {name: bob}}
""".strip())
    assert not _has(clean, "cond-referent-type")


# ── rule 6: cond-value-type-mismatch ─────────────────────────────────


def test_cond_value_type_mismatch():
    fires = _lint_set_yaml("""
fields:
  - {key: lg, type: checkbox, default: true}
  - {key: n, type: number, default: 1}
  - {key: t, type: text, show_if: {lg: maybe}}
  - {key: u, type: text, show_if: {n: abc}}
""".strip())
    assert _has(fires, "cond-value-type-mismatch", Level.WARNING)

    clean = _lint_set_yaml("""
fields:
  - {key: lg, type: checkbox, default: true}
  - {key: n, type: number, default: 1}
  - {key: t, type: text, show_if: {lg: true}}
  - {key: u, type: text, show_if: {n: 5}}
""".strip())
    assert not _has(clean, "cond-value-type-mismatch")


# ── rule 7: combinator structure (any/all/not are v2, not reserved) ──


def test_cond_combinator_valid_v2():
    # any/all/not are real combinators now — a well-formed one lints clean.
    clean = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [oneshot, chat, term]}
  - {key: backend, type: dropdown, options: [pai, claude]}
  - {key: a, type: text, required_if: {any: [{mode: term}, {mode: [oneshot, chat], backend: claude}]}}
""".strip())
    assert not _has(clean, "cond-combinator-mixed")
    assert not _has(clean, "cond-combinator-arg")
    assert not _has(clean, "cond-unknown-field")


def test_cond_combinator_arg_and_mixed():
    # any/all need a non-empty list; a combinator can't mix with field keys.
    bad_arg = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [chat]}
  - {key: a, type: text, show_if: {any: {mode: chat}}}
""".strip())
    assert _has(bad_arg, "cond-combinator-arg", Level.ERROR)

    mixed = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [chat]}
  - {key: a, type: text, show_if: {any: [{mode: chat}], mode: chat}}
""".strip())
    assert _has(mixed, "cond-combinator-mixed", Level.ERROR)


def test_cond_combinator_items_must_be_condition_maps():
    # A non-map or empty item inside any/all evaluates to False at runtime,
    # so it is a structure error rather than a silently dead branch.
    for body in (
        """
fields:
  - {key: mode, type: dropdown, options: [chat]}
  - {key: a, type: text, show_if: {any: [mode, {}]}}
""",
        """
fields:
  - {key: mode, type: dropdown, options: [chat]}
  - {key: a, type: text, required_if: {all: [{}]}}
""",
        """
fields:
  - {key: m, type: dropdown, options: [{value: a, enable_if: {any: [x]}}, b]}
""",
    ):
        assert _has(_lint_set_yaml(body.strip()), "cond-combinator-arg", Level.ERROR), body


def test_cond_not_forbidden_in_show_if():
    # `not` breaks the monotone visibility fixpoint -> banned in show_if…
    banned = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [chat]}
  - {key: a, type: text, show_if: {not: {mode: chat}}}
""".strip())
    assert _has(banned, "cond-not-in-show-if", Level.ERROR)

    # …but allowed in required_if (evaluated after the fixpoint converges).
    ok = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [chat]}
  - {key: a, type: text, required_if: {not: {mode: chat}}}
""".strip())
    assert not _has(ok, "cond-not-in-show-if")


def test_cond_field_named_reserved_key():
    # A field literally NAMED a combinator key still collides.
    fires_name = _lint_set_yaml("""
fields:
  - {key: all, type: checkbox, default: false}
""".strip())
    assert _has(fires_name, "cond-reserved-key", Level.ERROR)


# ── rule 8: required-if-on-checkbox ──────────────────────────────────


def test_required_if_on_checkbox():
    fires = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [chat]}
  - {key: flag, type: checkbox, default: false, show_if: {mode: chat}, required_if: {mode: chat}}
""".strip())
    assert _has(fires, "required-if-on-checkbox", Level.WARNING)

    clean = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [chat]}
  - {key: t, type: text, show_if: {mode: chat}, required_if: {mode: chat}}
""".strip())
    assert not _has(clean, "required-if-on-checkbox")


# ── rule 9: required-with-show_if ────────────────────────────────────


def test_required_with_show_if():
    fires = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [chat]}
  - {key: t, type: text, required: true, show_if: {mode: chat}}
""".strip())
    assert _has(fires, "required-with-show_if", Level.WARNING)

    clean = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [chat]}
  - {key: t, type: text, show_if: {mode: chat}, required_if: {mode: chat}}
""".strip())
    assert not _has(clean, "required-with-show_if")


# ── rule 10: required-if-permanently-hidden ──────────────────────────


def test_required_if_permanently_hidden():
    fires = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [chat, oneshot]}
  - {key: t, type: text, show_if: {mode: chat}, required_if: {mode: oneshot}}
""".strip())
    assert _has(fires, "required-if-permanently-hidden", Level.WARNING)

    # Overlapping operand sets ⇒ satisfiable ⇒ no flag (conservative).
    clean = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [chat, oneshot]}
  - {key: t, type: text, show_if: {mode: [chat, oneshot]}, required_if: {mode: oneshot}}
""".strip())
    assert not _has(clean, "required-if-permanently-hidden")


# ── rule 11: required-if-reference-superset ──────────────────────────


def test_required_if_reference_superset():
    # required_if references a field the show_if does not.
    fires = _lint_set_yaml("""
fields:
  - {key: a, type: dropdown, options: [x]}
  - {key: b, type: dropdown, options: [y]}
  - {key: t, type: text, show_if: {a: x}, required_if: {a: x, b: y}}
""".strip())
    assert _has(fires, "required-if-reference-superset", Level.ERROR)

    # No show_if, but the required_if referent is itself conditionally shown.
    fires2 = _lint_set_yaml("""
fields:
  - {key: a, type: dropdown, options: [x]}
  - {key: b, type: dropdown, options: [y], show_if: {a: x}}
  - {key: t, type: text, required_if: {b: y}}
""".strip())
    assert _has(fires2, "required-if-reference-superset", Level.ERROR)

    # The shipped shape: required_if references exactly its show_if fields.
    clean = _lint_set_yaml("""
fields:
  - {key: extras, type: checkbox, default: true}
  - {key: auth, type: dropdown, options: [api_key, oauth]}
  - {key: key, type: text, show_if: {extras: true, auth: api_key}, required_if: {extras: true, auth: api_key}}
""".strip())
    assert not _has(clean, "required-if-reference-superset")


def test_reference_superset_reads_combinator_show_if_referents():
    # A combinator show_if's referents are its leaf keys, not 'any'/'all'.
    clean = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [a, b]}
  - {key: x, type: text, show_if: {any: [{mode: a}, {mode: b}]}, required_if: {mode: a}}
""".strip())
    assert not _has(clean, "required-if-reference-superset")

    # A referent absent from every branch still fires.
    fires = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [a, b]}
  - {key: other, type: dropdown, options: [x], show_if: {mode: b}}
  - {key: t, type: text, show_if: {any: [{mode: a}]}, required_if: {other: x}}
""".strip())
    assert _has(fires, "required-if-reference-superset", Level.ERROR)

    # Same for an option enable_if gating on a combinator show_if referent.
    enable = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [a, b]}
  - {key: p, type: checkbox, default: false, show_if: {mode: b}}
  - {key: m, type: dropdown, show_if: {any: [{mode: a}, {p: true}]}, options: [{value: a, enable_if: {p: true}}, b]}
""".strip())
    assert not _has(enable, "enable-if-reference-superset")


# ── rule 12: cond-empty ──────────────────────────────────────────────


def test_cond_empty():
    fires_empty = _lint_set_yaml("""
fields:
  - {key: t, type: text, show_if: {}}
""".strip())
    assert _has(fires_empty, "cond-empty", Level.ERROR)

    fires_nonmap = _lint_set_yaml("""
fields:
  - {key: t, type: text, required_if: 5}
""".strip())
    assert _has(fires_nonmap, "cond-empty", Level.ERROR)

    clean = _lint_set_yaml("""
fields:
  - {key: mode, type: dropdown, options: [chat]}
  - {key: t, type: text, show_if: {mode: chat}}
""".strip())
    assert not _has(clean, "cond-empty")


# ── rule 13: enable-if-unknown-field ─────────────────────────────────


def test_enable_if_unknown_field():
    fires = _lint_set_yaml("""
fields:
  - {key: m, type: dropdown, options: [{value: a, enable_if: {nope: x}}, b]}
""".strip())
    assert _has(fires, "enable-if-unknown-field", Level.ERROR)

    clean = _lint_set_yaml("""
fields:
  - {key: p, type: dropdown, options: [x, y]}
  - {key: m, type: dropdown, options: [{value: a, enable_if: {p: x}}, b]}
""".strip())
    assert not _has(clean, "enable-if-unknown-field")


# ── rule 14: enable-if-referent-type ─────────────────────────────────


def test_enable_if_referent_type():
    fires = _lint_set_yaml("""
fields:
  - {key: t, type: text}
  - {key: m, type: dropdown, options: [{value: a, enable_if: {t: x}}, b]}
""".strip())
    assert _has(fires, "enable-if-referent-type", Level.ERROR)

    clean = _lint_set_yaml("""
fields:
  - {key: p, type: dropdown, options: [x]}
  - {key: m, type: dropdown, options: [{value: a, enable_if: {p: x}}, b]}
""".strip())
    assert not _has(clean, "enable-if-referent-type")


# ── rule 15: enable-if-value-not-an-option ───────────────────────────


def test_enable_if_value_not_an_option():
    fires = _lint_set_yaml("""
fields:
  - {key: p, type: dropdown, options: [x, y]}
  - {key: m, type: dropdown, options: [{value: a, enable_if: {p: zzz}}, b]}
""".strip())
    assert _has(fires, "enable-if-value-not-an-option", Level.ERROR)

    clean = _lint_set_yaml("""
fields:
  - {key: p, type: dropdown, options: [x, y]}
  - {key: m, type: dropdown, options: [{value: a, enable_if: {p: x}}, b]}
""".strip())
    assert not _has(clean, "enable-if-value-not-an-option")


# ── rule 16: enable-if-default-disabled ──────────────────────────────


def test_enable_if_default_disabled():
    # At defaults p=false ⇒ option 'a' disabled, yet the field defaults to 'a'.
    fires = _lint_set_yaml("""
fields:
  - {key: p, type: checkbox, default: false}
  - {key: m, type: dropdown, default: a, options: [{value: a, enable_if: {p: true}}, b]}
""".strip())
    assert _has(fires, "enable-if-default-disabled", Level.ERROR)

    # p=true ⇒ 'a' enabled at defaults ⇒ no problem.
    clean = _lint_set_yaml("""
fields:
  - {key: p, type: checkbox, default: true}
  - {key: m, type: dropdown, default: a, options: [{value: a, enable_if: {p: true}}, b]}
""".strip())
    assert not _has(clean, "enable-if-default-disabled")


# ── rule 17: enable-if-empty ─────────────────────────────────────────


def test_enable_if_empty():
    fires = _lint_set_yaml("""
fields:
  - {key: m, type: dropdown, options: [{value: a, enable_if: {}}, b]}
""".strip())
    assert _has(fires, "enable-if-empty", Level.ERROR)

    clean = _lint_set_yaml("""
fields:
  - {key: p, type: dropdown, options: [x]}
  - {key: m, type: dropdown, options: [{value: a, enable_if: {p: x}}, b]}
""".strip())
    assert not _has(clean, "enable-if-empty")


# ── rule 18: enable-if-on-non-dropdown ───────────────────────────────


def test_enable_if_on_non_dropdown():
    # enable_if option on a non-dropdown field.
    fires_type = _lint_set_yaml("""
fields:
  - {key: p, type: dropdown, options: [x]}
  - {key: m, type: text, options: [{value: a, enable_if: {p: x}}]}
""".strip())
    assert _has(fires_type, "enable-if-on-non-dropdown", Level.ERROR)

    # object option carrying enable_if but no 'value'.
    fires_novalue = _lint_set_yaml("""
fields:
  - {key: p, type: dropdown, options: [x]}
  - {key: m, type: dropdown, options: [{label: A, enable_if: {p: x}}]}
""".strip())
    assert _has(fires_novalue, "enable-if-on-non-dropdown", Level.ERROR)

    clean = _lint_set_yaml("""
fields:
  - {key: p, type: dropdown, options: [x]}
  - {key: m, type: dropdown, options: [{value: a, enable_if: {p: x}}, b]}
""".strip())
    assert not _has(clean, "enable-if-on-non-dropdown")


# ── rule 19: enable_if v2 combinators (any/all/not, not reserved) ────


def test_enable_if_combinator():
    # A malformed combinator (any without a list) is a structure error…
    bad = _lint_set_yaml("""
fields:
  - {key: m, type: dropdown, options: [{value: a, enable_if: {any: x}}, b]}
""".strip())
    assert _has(bad, "cond-combinator-arg", Level.ERROR)

    # …a well-formed one lints clean, incl. `not` (enable_if is post-fixpoint)…
    clean = _lint_set_yaml("""
fields:
  - {key: p, type: dropdown, options: [x, y]}
  - {key: q, type: dropdown, options: [x, y]}
  - {key: m, type: dropdown, options: [{value: a, enable_if: {any: [{p: x}, {not: {q: y}}]}}, b]}
""".strip())
    assert not _has(clean, "cond-combinator-arg")
    assert not _has(clean, "cond-combinator-mixed")
    assert not _has(clean, "cond-not-in-show-if")
    assert not _has(clean, "enable-if-unknown-field")


# ── rule 21: enable-if-reference-superset ────────────────────────────


def test_enable_if_reference_superset():
    # 'g' is conditionally shown and not in m's show_if ⇒ warn.
    fires = _lint_set_yaml("""
fields:
  - {key: z, type: dropdown, options: [q]}
  - {key: g, type: dropdown, options: [x], show_if: {z: q}}
  - {key: m, type: dropdown, options: [{value: a, enable_if: {g: x}}, b]}
""".strip())
    assert _has(fires, "enable-if-reference-superset", Level.WARNING)

    # Referent 'z' is statically shown ⇒ no warn.
    clean = _lint_set_yaml("""
fields:
  - {key: z, type: dropdown, options: [q]}
  - {key: m, type: dropdown, options: [{value: a, enable_if: {z: q}}, b]}
""".strip())
    assert not _has(clean, "enable-if-reference-superset")

    # Equal-visibility exemption: referent 'g' and owner 'm' carry the SAME
    # folded show_if, so they hide/show together — the referent can never be
    # hidden while the option is offered ⇒ no warn.
    same_vis = _lint_set_yaml("""
fields:
  - {key: z, type: dropdown, options: [q, r]}
  - {key: g, type: dropdown, options: [x, y], show_if: {z: q}}
  - {key: m, type: dropdown, show_if: {z: q}, options: [{value: a, enable_if: {g: x}}, b]}
""".strip())
    assert not _has(same_vis, "enable-if-reference-superset")

    # ...but a merely-overlapping (not equal) show_if still warns.
    diff_vis = _lint_set_yaml("""
fields:
  - {key: z, type: dropdown, options: [q, r]}
  - {key: g, type: dropdown, options: [x, y], show_if: {z: q}}
  - {key: m, type: dropdown, show_if: {z: [q, r]}, options: [{value: a, enable_if: {g: x}}, b]}
""".strip())
    assert _has(diff_vis, "enable-if-reference-superset", Level.WARNING)


# yaml is imported lazily by the helper above — make it importable at
# module level so the test file's other tests still work if yaml isn't
# already imported.
import yaml  # noqa: E402


# ---------------------------------------------------------------------------
# The runner grant's commands axis
# ---------------------------------------------------------------------------

_GENERATORS = GENERATORS


def _shipped_ref(generator: str):
    """The linter's view of a shipped generator (its manifest's GeneratorRef)."""
    from collimate.lint.core import _read_gen_ref
    manifest = next(_GENERATORS.glob(f"*/{generator}/*.generator"))
    return _read_gen_ref(manifest)[1]


def _agent_runner(commands=None, *, docker="none", backend="pydantic-ai",
                  runner_id="r", generator="agent", surface="annotation"):
    from collimate.lint import lint_runner_manifest_data
    perms = {"docker": docker, "internet": "full"}
    if commands is not None:
        perms["commands"] = commands
    data = {"id": runner_id, "name": "R", "description": "d", "generator": generator,
            "prompt": "", "settings": {"agent-config.set_yaml": {
                "surface": surface, "backend": backend}},
            "folders": [], "accounts": {}, "permissions": perms, "packages": {}}
    ref = _shipped_ref(generator)
    issues = lint_runner_manifest_data(data, resolve=lambda gid: ref if gid == generator else None)
    return {(i.level.name, i.code) for i in issues}


def test_runner_commands_axis_is_an_enum():
    for level in ("deny", "ask", "allow"):
        assert ("ERROR", "runner-permission-bad-enum") not in _agent_runner(level, docker="full")
    assert ("ERROR", "runner-permission-bad-enum") in _agent_runner("yolo")
    assert ("ERROR", "runner-permission-bad-enum") in _agent_runner(True)
    assert ("ERROR", "runner-permission-unknown-key") not in _agent_runner("deny")


def test_runner_commands_warns_where_nothing_can_run_them():
    """pydantic-ai is a host adapter whose commands run on this machine: no
    commands level needs docker. Only docker: limited (a process on this
    machine cannot be contained by it) cannot serve it, and an Annotation run
    adopts full at start, so only a chat (or CLI) runner, whose grant is the
    user's explicit choice, is warned about."""
    code = ("WARNING", "runner-grant-cannot-serve")
    assert code not in _agent_runner("ask", docker="none", surface="chat")
    assert code in _agent_runner("allow", docker="limited", surface="chat")
    assert code not in _agent_runner("ask", docker="none")
    assert code not in _agent_runner("allow", docker="full")
    assert code not in _agent_runner("deny", docker="none")
    assert code not in _agent_runner("allow", docker="full", backend="codex")


def test_runner_commands_warns_about_unattended_host_commands():
    code = ("WARNING", "runner-commands-unattended-host")
    assert code in _agent_runner("allow", docker="none", backend="codex")
    assert code not in _agent_runner("ask", docker="none", backend="codex")
    assert code not in _agent_runner("allow", docker="full", backend="codex")


def test_the_built_in_harness_sandboxes_its_commands_under_docker_full():
    """pydantic-ai's commands run in its sandbox container under docker:
    full, so allow is unattended on this machine only below it."""
    host = ("WARNING", "runner-commands-unattended-host")
    assert host in _agent_runner("allow", docker="none")
    assert host not in _agent_runner("allow", docker="full")
    assert host not in _agent_runner("ask", docker="none")
    assert host not in _agent_runner("deny", docker="none")


def test_runner_commands_warns_about_claude_codes_host_chat_leg():
    """The diagnostic flags Claude host commands at every requested grant;
    an impossible limited grant independently warns that the runner cannot start."""
    code = ("WARNING", "runner-commands-unattended-host")
    for docker in ("none", "limited", "full"):
        assert code in _agent_runner("allow", backend="claude-code", docker=docker)
        assert code not in _agent_runner("ask", backend="claude-code", docker=docker)
        assert code not in _agent_runner("deny", backend="claude-code", docker=docker)
    assert code in _agent_runner("allow", docker="full", backend="claude-code",
                                 surface="terminal")


def _agent_runner_issues(commands, *, docker, backend, surface="terminal"):
    from collimate.lint import lint_runner_manifest_data
    data = {"id": "r", "name": "R", "description": "d", "generator": "agent", "prompt": "",
            "settings": {"agent-config.set_yaml": {"surface": surface, "backend": backend}},
            "folders": [], "accounts": {}, "packages": {},
            "permissions": {"docker": docker, "internet": "full", "commands": commands}}
    ref = _shipped_ref("agent")
    return lint_runner_manifest_data(data, resolve=lambda gid: ref)


def test_the_cli_surface_is_linted_where_its_terminal_locus_puts_it():
    """Claude Code's CLI runs on this machine: no docker needed, but
    commands: deny and docker: limited cannot serve it. Antigravity's CLI is
    on this machine on the Google plan and in its container on a key — a
    runner names neither, so each auth's refusal is reported with the auth
    it holds for. A container CLI still needs docker: full."""
    def warned(*args, **kw):
        return [i.message for i in _agent_runner_issues(*args, **kw) if i.code == "runner-grant-cannot-serve"]

    def codes(*args, **kw):
        return {i.code for i in _agent_runner_issues(*args, **kw)}

    assert "runner-docker-required" not in codes("ask", docker="none", backend="claude-code")
    assert warned("ask", docker="none", backend="claude-code") == []
    assert any("commands: deny" in m for m in warned("deny", docker="none", backend="claude-code"))
    assert any("docker: limited" in m for m in warned("ask", docker="limited", backend="claude-code"))
    agy = warned("ask", docker="none", backend="antigravity")
    assert agy and all(m.endswith("(on api_key)") and "needs docker: full" in m for m in agy)
    agy = warned("deny", docker="full", backend="antigravity")
    assert agy and all(m.endswith("(on google)") and "commands: deny" in m for m in agy)
    assert "runner-docker-required" not in codes("ask", docker="none", backend="antigravity")
    assert "runner-docker-required" in codes("ask", docker="none", backend="codex")
    assert ("WARNING", "runner-commands-unattended-host") in _agent_runner(
        "allow", docker="full", backend="antigravity", surface="terminal")


# The shipped presets' commands levels, by (generator, preset): the
# built-in harness's presets deny, except the two whose work runs commands in
# its docker: full sandbox container, which allow; claude-code's document and
# chat turns run on this machine, so its preset denies; the container harness
# presets allow. A generator that does not declare `enforces: [commands]`
# (launch, web) carries no level.
from collimate.account_taxonomy import HARNESS_CONTAINERIZED as _CONTAINER_HARNESSES  # noqa: E402
_PRESET_COMMANDS = {
    ("agent", "challenge"): "allow", ("agent", "superpowers"): "allow",
    ("agent", "claude-code"): "deny",
    **{("agent", harness): "allow" for harness in _CONTAINER_HARNESSES},
}


def _preset_manifests() -> list:
    root = GENERATORS
    runners = sorted(root.glob("*/*/*.runner"))
    configs = sorted(root.glob("*/*/configs/*/manifest.config_yaml"))
    return [pytest.param(p, id=f"{p.parent.name}.{p.suffix[1:]}") for p in runners + configs]


@pytest.mark.parametrize("path", _preset_manifests())
def test_every_preset_ships_the_planned_commands_level(path):
    data = yaml.safe_load(path.read_text())
    # A runner names its generator; a config sits inside it.
    generator = (data["generator"] if path.suffix == ".runner"
                 else path.parents[2].name)
    perms = data.get("permissions") or {}
    if "commands" not in _shipped_ref(generator).enforces:
        assert "commands" not in perms
        return
    preset = path.parent.name
    if (generator, preset) in _PRESET_COMMANDS:
        want = _PRESET_COMMANDS[(generator, preset)]
    else:
        # Everything else is an Agent-engine preset without a sandbox.
        backends = {v.get("backend") for v in (data.get("settings") or {}).values()
                    if isinstance(v, dict)}
        assert backends == {"pydantic-ai"} and perms.get("docker") == "none", preset
        want = "deny"
    assert perms.get("commands") == want, preset


# ---------------------------------------------------------------------------
# enforces — the grant axes a generator enforces itself
# ---------------------------------------------------------------------------


def _gen_manifest(**extra):
    return {"id": "g", "name": "G", "summary": "s", "entrypoint": "main.py", "main_config": "cfg.set_yaml", **extra}


def _gen_codes(**extra) -> set:
    issues = lint_generator_manifest_data(_gen_manifest(**extra), files_present={"main.py", "cfg.set_yaml"})
    return {(i.level.name, i.code) for i in issues}


def test_enforces_names_only_axes_the_host_cannot_enforce():
    assert _gen_codes(enforces=["commands"]) == set()
    assert ("ERROR", "enforces-axis") in _gen_codes(enforces=["shell"])
    for host_axis in ("docker", "internet", "update_packages", "internet_allow"):
        assert ("ERROR", "enforces-host-axis") in _gen_codes(enforces=[host_axis]), host_axis
    assert ("ERROR", "enforces") in _gen_codes(enforces="commands")
    assert ("ERROR", "enforces") in _gen_codes(enforces=["commands", "commands"])
    assert ("ERROR", "unknown-field") not in _gen_codes(enforces=["commands"])


def test_declared_enforces_keeps_only_generator_axes():
    from collimate.lint import declared_enforces
    assert declared_enforces({"enforces": ["commands"]}) == frozenset({"commands"})
    assert declared_enforces({"enforces": ["docker", "commands", "bogus"]}) == frozenset({"commands"})
    assert declared_enforces({"enforces": "commands"}) == frozenset()
    assert declared_enforces({}) == frozenset()


def test_the_shipped_agent_declares_commands_and_launch_and_web_do_not():
    assert _shipped_ref("agent").enforces == frozenset({"commands"})
    assert _shipped_ref("launch").enforces == frozenset()
    assert _shipped_ref("web").enforces == frozenset()


def test_a_generator_ref_must_say_what_its_target_enforces():
    """``enforces`` has no default: a host that builds a GeneratorRef without
    reading the manifest's declaration fails at once, rather than silently
    skipping the commands checks an enforcing target needs."""
    with pytest.raises(TypeError, match="enforces"):
        GeneratorRef(main_config="settings.set_yaml")
    assert GeneratorRef(enforces=frozenset({"commands"})).enforces == {"commands"}


def test_a_commands_level_on_a_generator_that_does_not_enforce_it_warns():
    """launch runs its own configured commands and IDE agents a human drives,
    so a `commands` level there has no effect: the runner still saves (a
    warning, not an error), and none of the agent's commands checks run."""
    code = ("WARNING", "runner-axis-not-enforced")
    for level in ("deny", "ask", "allow"):
        got = _agent_runner(level, generator="launch", backend="claude-code")
        assert code in got, level
        assert not {c for c in got if c[1].startswith("runner-commands-")}, level
    assert code not in _agent_runner(None, generator="launch")
    # The agent enforces it: no such warning, its own checks run.
    assert code not in _agent_runner("allow", backend="claude-code")
    assert ("WARNING", "runner-commands-unattended-host") in _agent_runner(
        "allow", backend="claude-code")


def test_an_unresolved_target_gets_no_generator_axis_checks():
    from collimate.lint import lint_runner_manifest_data
    data = {"id": "r", "name": "R", "description": "d", "generator": "agent", "prompt": "",
            "settings": {"agent-config.set_yaml": {"backend": "claude-code"}},
            "folders": [], "accounts": {}, "packages": {},
            "permissions": {"docker": "none", "internet": "full", "commands": "allow"}}
    codes = {i.code for i in lint_runner_manifest_data(data)}
    assert not {c for c in codes if c.startswith("runner-commands-") or c == "runner-axis-not-enforced"}


def _allowlist_codes(allow, internet="proxy") -> set:
    from collimate.lint import lint_runner_manifest_data
    data = {"id": "r", "name": "R", "description": "d", "generator": "g", "prompt": "",
            "settings": {}, "folders": [], "accounts": {}, "packages": {},
            "permissions": {"docker": "limited", "internet": internet, "internet_allow": allow}}
    return {(i.level.name, i.code) for i in lint_runner_manifest_data(data)}


def test_internet_allow_entries_that_never_match_warn__F2041():
    """A wildcard, scheme, path, port or inner space never equals a host, so
    the proxy denies it without saying why: the linter says so at authoring
    time. A bare host (any case, leading dot, surrounding space) is fine."""
    code = ("WARNING", "runner-internet-allow-entry")
    for entry in ("*.example.com", "https://api.example.com", "example.com/v1",
                  "api.example.com:443", "api example.com"):
        assert code in _allowlist_codes([entry]), entry
    assert code not in _allowlist_codes(["api.example.com", ".Example.COM", " a.com "])
    # Outside proxy mode the whole list is unused, which is the one warning.
    assert code not in _allowlist_codes(["*.example.com"], internet="full")
    assert ("WARNING", "runner-internet-allow-unused") in _allowlist_codes(
        ["*.example.com"], internet="full")


def test_a_config_of_a_non_enforcing_generator_warns_about_commands(tmp_path):
    (tmp_path / "main.py").write_text("")
    (tmp_path / "settings.set_yaml").write_text("fields:\n- {key: x, type: text, default: ''}\n")
    cfg = tmp_path / "configs" / "default"
    cfg.mkdir(parents=True)
    (cfg / "manifest.config_yaml").write_text(yaml.safe_dump({
        "name": "Default", "prompt": "", "settings": {"settings.set_yaml": {"x": ""}},
        "folders": [], "accounts": {}, "packages": {},
        "permissions": {"docker": "none", "commands": "deny"}}))
    manifest = tmp_path / "custom.generator"
    base = {"id": "g", "name": "G", "summary": "s", "entrypoint": "main.py",
            "main_config": "settings.set_yaml", "draft_files": ["settings.set_yaml"],
            "configs": [{"id": "default", "name": "Default",
                         "path": "configs/default/manifest.config_yaml"}]}
    manifest.write_text(yaml.safe_dump(base))
    codes = {(i.level.name, i.code) for i in lint_path(manifest)}
    assert ("WARNING", "runner-axis-not-enforced") in codes
    manifest.write_text(yaml.safe_dump({**base, "enforces": ["commands"]}))
    codes = {(i.level.name, i.code) for i in lint_path(manifest)}
    assert ("WARNING", "runner-axis-not-enforced") not in codes


def test_generator_lint_walks_the_directory_once(tmp_path, monkeypatch):
    """The folder-content check reuses one directory listing per lint rather
    than re-walking the tree for every config and folder slot."""
    (tmp_path / "main.py").write_text("")
    (tmp_path / "settings.set_yaml").write_text("fields:\n- {key: x, type: text, default: ''}\n")
    configs = []
    for cid in ("one", "two"):
        cfg = tmp_path / "configs" / cid
        for folder in ("skills", "mcp"):
            (cfg / "files" / folder).mkdir(parents=True)
            (cfg / "files" / folder / "f.txt").write_text("x")
        (cfg / "manifest.config_yaml").write_text(yaml.safe_dump({
            "name": cid, "prompt": "", "settings": {"settings.set_yaml": {"x": ""}},
            "folders": ["skills", "mcp"], "accounts": {}, "packages": {},
            "permissions": {"docker": "none"}}))
        configs.append({"id": cid, "name": cid,
                        "path": f"configs/{cid}/manifest.config_yaml"})
    manifest = tmp_path / "custom.generator"
    manifest.write_text(yaml.safe_dump({
        "id": "g", "name": "G", "summary": "s", "entrypoint": "main.py",
        "main_config": "settings.set_yaml", "draft_files": ["settings.set_yaml"],
        "config_folders": [{"name": "skills", "description": "d"},
                           {"name": "mcp", "description": "d"}],
        "configs": configs}))
    calls = []
    real_rglob = Path.rglob

    def counting_rglob(self, pattern, *a, **k):
        calls.append(self)
        return real_rglob(self, pattern, *a, **k)

    monkeypatch.setattr(Path, "rglob", counting_rglob)
    issues = lint_generator_manifest(manifest)
    assert not [i for i in issues if i.code == "runner-folder-empty"], issues
    assert len(calls) == 1


# ── ui_files: the generator's UI file suffixes ───────────────────────


def test_ui_files_lists_filename_suffixes():
    assert _gen_codes(ui_files=[".chat", ".json", ".comments.yaml"]) == set()
    assert _gen_codes(ui_files=[]) == set()
    assert _gen_codes() == set(), "ui_files is optional"


@pytest.mark.parametrize("value, code", [
    (".chat", "bad-type"),
    ({".chat": True}, "bad-type"),
    (["chat"], "ui-files-suffix"),
    (["."], "ui-files-suffix"),
    ([""], "ui-files-suffix"),
    ([3], "ui-files-suffix"),
    ([".a/b"], "ui-files-suffix"),
    ([".run"], "ui-files-run-card"),
])
def test_ui_files_refuses_what_is_not_a_suffix_list(value, code):
    assert ("ERROR", code) in _gen_codes(ui_files=value)



def test_runner_value_types_come_from_the_declared_main_config(tmp_path):
    """Runner settings read field types from the declared main config."""
    gen = tmp_path / "g"
    gen.mkdir()
    (gen / "main.py").write_text("")
    (gen / "a.set_yaml").write_text("fields:\n- {key: x, type: number, default: 1}\n")
    (gen / "b.set_yaml").write_text("fields:\n- {key: x, type: text, default: ''}\n")
    (gen / "custom.generator").write_text(yaml.safe_dump({
        "id": "g", "name": "G", "summary": "s", "entrypoint": "main.py",
        "main_config": "a.set_yaml", "draft_files": ["a.set_yaml", "b.set_yaml"]}))
    runner = tmp_path / "r"
    runner.mkdir()
    (runner / "custom.runner").write_text(yaml.safe_dump({
        "id": "r", "name": "R", "generator": "g",
        "settings": {"a.set_yaml": {"x": 3}}}))
    codes = {i.code for i in lint_path(tmp_path)}
    assert "runner-value-type" not in codes


def test_grant_routing_warnings_use_actual_runner_permissions__D06():
    from collimate.lint import lint_runner_manifest_data
    ref = _shipped_ref("agent")
    def codes(backend, docker, commands="allow", **settings):
        data = {"id": "d06", "name": "Grant", "description": "", "generator": "agent",
                "prompt": "", "settings": {"agent-config.set_yaml": {"backend": backend, **settings}},
                "folders": [], "accounts": {}, "packages": {},
                "permissions": {"docker": docker, "internet": "full", "commands": commands}}
        return {i.code for i in lint_runner_manifest_data(data, resolve=lambda _: ref)}
    assert "runner-grant-cannot-serve" not in codes("pydantic-ai", "none", surface="chat")
    assert "runner-grant-cannot-serve" in codes("pydantic-ai", "limited", surface="chat")
    assert "runner-grant-cannot-serve" in codes("codex", "limited", surface="chat")
    assert "runner-grant-cannot-serve" in codes("pi", "none", surface="chat")
    # The Annotation surface adopts its harness's minimum grant.
    assert "runner-grant-cannot-serve" not in codes("pi", "none", surface="annotation")
    assert "runner-grant-cannot-serve" in codes("claude-code", "full", mcp="http_stdio")
    assert "runner-commands-unattended-host" in codes("codex", "none")
    assert "runner-commands-unattended-host" not in codes("codex", "full")
    assert "runner-grant-cannot-serve" not in codes("pydantic-ai", "none", commands="deny")


def test_only_an_opted_in_generator_is_linted_against_the_adopted_grant():
    """The Agent declares ``adopts_harness_grant: true``, so its Annotation
    runner is checked against the grant the run is raised to; a generator
    without the opt-in is checked against its runner's own grant, and a host
    harness under ``limited`` is never raised (``none`` already places it)."""
    import dataclasses
    from collimate.lint import lint_runner_manifest_data
    agent = _shipped_ref("agent")
    assert agent.adopts_harness_grant is True
    third_party = dataclasses.replace(agent, adopts_harness_grant=False)

    def codes(ref, backend, docker, commands="deny"):
        data = {"id": "adopt", "name": "Adopt", "description": "", "generator": "agent", "prompt": "",
                "settings": {"agent-config.set_yaml": {"backend": backend, "surface": "annotation"}},
                "folders": [], "accounts": {}, "packages": {},
                "permissions": {"docker": docker, "internet": "full", "commands": commands}}
        return {i.code for i in lint_runner_manifest_data(data, resolve=lambda _: ref)}
    # A container harness on docker: none: the Agent's run is raised to full.
    assert "runner-grant-cannot-serve" not in codes(agent, "pi", "none")
    assert "runner-grant-cannot-serve" in codes(third_party, "pi", "none")
    # claude-code and pydantic-ai run on this machine: limited is refused,
    # never raised, for the Agent too.
    assert "runner-grant-cannot-serve" in codes(agent, "claude-code", "limited")
    assert "runner-grant-cannot-serve" in codes(agent, "pydantic-ai", "limited")
    assert "runner-grant-cannot-serve" not in codes(agent, "claude-code", "none")


def test_a_runner_of_a_requires_docker_generator_needs_docker_full():
    """A generator whose every run provisions a container declares
    ``requires_docker``: a runner of it without ``docker: full`` is an
    error when it is authored, not a run that fails later."""
    from collimate.lint import lint_runner_manifest_data

    def codes(docker, **manifest):
        ref = GeneratorRef.from_manifest(_gen_manifest(**manifest))
        data = {"id": "r", "name": "R", "description": "d", "generator": "g", "prompt": "",
                "settings": {}, "folders": [], "accounts": {}, "packages": {},
                "permissions": {"docker": docker, "internet": "full"}}
        issues = lint_runner_manifest_data(data, resolve=lambda gid: ref if gid == "g" else None)
        return {(i.level.name, i.code) for i in issues}

    code = ("ERROR", "runner-docker-required")
    assert code in codes("none", requires_docker=True)
    assert code in codes("limited", requires_docker=True)
    assert code not in codes("full", requires_docker=True)
    assert code not in codes("none")


def test_requires_docker_if_needs_docker_full_only_where_it_holds_GN02():
    """``requires_docker_if`` is evaluated against the runner's settings over
    the main config's defaults: only a runner whose settings meet it needs
    ``docker: full``."""
    from collimate.lint import lint_runner_manifest_data

    settings_yaml = ("fields:\n"
                     "  - {key: surface, type: dropdown, options: [file, chat, terminal], default: file}\n")
    manifest = _gen_manifest(requires_docker_if={"surface": "terminal"})
    ref = GeneratorRef.from_manifest(manifest, read_file=lambda _: settings_yaml)

    def codes(docker, surface):
        data = {"id": "r", "name": "R", "description": "d", "generator": "g", "prompt": "",
                "settings": {"cfg.set_yaml": {"surface": surface}}, "folders": [],
                "accounts": {}, "packages": {}, "permissions": {"docker": docker, "internet": "full"}}
        return {(i.level.name, i.code) for i in lint_runner_manifest_data(data, resolve=lambda _: ref)}

    code = ("ERROR", "runner-docker-required")
    assert code in codes("none", "terminal")
    assert code not in codes("full", "terminal")
    assert code not in codes("none", "file")
    assert code not in codes("none", "chat")
    # The generator's own lint knows the key and checks its referents.
    issues = lint_generator_manifest_data(manifest, files_present={"main.py", "cfg.set_yaml"},
                                          file_contents={"cfg.set_yaml": settings_yaml})
    assert not [i for i in issues if i.level.name == "ERROR"]
    issues = lint_generator_manifest_data(_gen_manifest(requires_docker_if={"backend": "x"}),
                                          files_present={"main.py", "cfg.set_yaml"},
                                          file_contents={"cfg.set_yaml": settings_yaml})
    assert "manifest-condition-unknown-field" in {i.code for i in issues}


def test_new_generator_manifest_keys_are_known_GN01_GN03():
    from collimate.lint import GENERATOR_FIELDS
    assert {"takes_selected_file", "takes_selected_file_if", "requires_docker_if",
            "opens"} <= GENERATOR_FIELDS
    assert ("ERROR", "bad-type") in _gen_codes(takes_selected_file="yes")
    assert ("ERROR", "bad-type") in _gen_codes(requires_docker="yes")
    assert ("ERROR", "bad-type") in _gen_codes(opens={"path": "x.website"})
    assert _gen_codes(takes_selected_file=True, opens=[{"path": "notes/plan.md"}]) == set()
