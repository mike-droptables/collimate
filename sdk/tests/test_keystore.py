"""collimate.keystore: the structure of a Key manager entry, never its values,
and gen.keystore_structures(), the read-only introspection a generator writing
a generator uses."""
from __future__ import annotations

import dataclasses
import json

import pytest

from collimate.keystore import (
    CustomStructure, Group, Key, KeystoreStructures, ShippedService, check_structure, leaves, same_structure,
    structure_from_leaves, structures_from_answer,
)

WANTED = {"url": "public", "auth": {"user": "public", "token": "secret"}}


def test_a_structure_is_checked_into_canonical_form():
    assert check_structure(WANTED) == {"auth": {"token": "secret", "user": "public"}, "url": "public"}
    assert list(check_structure(WANTED)) == ["auth", "url"]
    assert leaves(check_structure(WANTED)) == (("auth.token", True), ("auth.user", False), ("url", False))
    assert structure_from_leaves([("url", False), ("auth.user", False), ("auth.token", True)]) == check_structure(WANTED)


@pytest.mark.parametrize("raw, message", [
    ({"url": "public"}, "at least one secret key"),
    ({}, "must map key names"),
    ({"token": "yes"}, "must be secret, public or a nested group"),
    ({"token": "secret", "group": {}}, "must map key names"),
    ({"1st": "secret"}, "is not a key name"),
    ({"a-b": "secret"}, "is not a key name"),
    ("token: secret", "must map key names"),
    ({"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": "secret"}}}}}}}}, "nests deeper than 6"),
    ({f"k{i}": "secret" for i in range(65)}, "more than 64 keys"),
])
def test_a_malformed_structure_is_refused(raw, message):
    with pytest.raises(ValueError, match=message):
        check_structure(raw)


@pytest.mark.parametrize("other, same", [
    ({"auth": {"token": "secret", "user": "public"}, "url": "public"}, True),          # another order
    ({"url": "public", "auth": {"user": "public", "api_token": "secret"}}, False),     # a key renamed
    ({"url": "secret", "auth": {"user": "public", "token": "secret"}}, False),         # a secret flag flipped
    ({"url": "public", "auth": {"user": "public", "token": "public"}}, False),         # the secret made public
    ({"url": "public", "user": "public", "token": "secret"}, False),                   # the nesting flattened
    ({"url": "public", "creds": {"user": "public", "token": "secret"}}, False),        # nested under another label
    ({**WANTED, "region": "public"}, False),                                           # an extra key
    ({"url": "public", "auth": {"token": "secret"}}, False),                           # a key missing
    ({"url": "public", "auth": "secret"}, False),                                      # a group made a key
])
def test_matching_is_exact_structure(other, same):
    assert same_structure(WANTED, other) is same
    assert same_structure(other, WANTED) is same


def test_a_malformed_structure_matches_nothing():
    assert not same_structure({"url": "public"}, {"url": "public"})
    assert not same_structure(None, WANTED)


def test_structure_from_leaves_refuses_a_key_that_is_also_a_group():
    with pytest.raises(ValueError, match="cannot hold others"):
        structure_from_leaves([("auth", True), ("auth.token", True)])
    with pytest.raises(ValueError, match="both a key and a group"):
        structure_from_leaves([("auth.token", True), ("auth", True)])


ANSWER = {
    "shipped": [{"kind": "model", "service": "openai", "name": "OpenAI", "protocol": "openai",
                 "auth_types": ["api_key", "cli"], "capabilities": ["text"]}],
    "custom": [check_structure(WANTED), {"token": "secret"}],
}


def _values(obj) -> list:
    """Every leaf value reachable from ``obj`` through dataclass fields and tuples."""
    if dataclasses.is_dataclass(obj):
        return [v for f in dataclasses.fields(obj) for v in _values(getattr(obj, f.name))]
    if isinstance(obj, tuple):
        return [v for item in obj for v in _values(item)]
    return [obj]


def test_the_answer_is_read_into_frozen_value_free_objects():
    structures = structures_from_answer(ANSWER)
    assert structures.shipped == (ShippedService("model", "openai", "OpenAI", "openai", ("api_key", "cli"), ("text",)),)
    first = structures.custom[0]
    assert first == CustomStructure((Group("auth", (Key("token", True), Key("user", False))), Key("url", False)))
    assert first.as_manifest() == check_structure(WANTED) and first.leaves()[0] == ("auth.token", True)
    assert structures.matching({"auth": {"token": "secret", "user": "public"}, "url": "public"}) is first
    assert structures.matching({"url": "public", "token": "secret"}) is None
    assert structures.matching("not a structure") is None
    # Nothing but names, enum strings and flags is reachable, and nothing can be set.
    assert all(isinstance(v, (str, bool)) for v in _values(structures))
    assert {v for v in _values(structures) if isinstance(v, str)} <= {
        "model", "openai", "OpenAI", "api_key", "cli", "text", "auth", "token", "user", "url"}
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.keys = ()  # type: ignore[misc]


@pytest.mark.parametrize("answer, message", [
    # A host bug that put an entry's values, names or ids in the answer is refused whole.
    ({**ANSWER, "custom": [{"url": "https://issues.example", "token": "secret"}]}, "must be secret, public"),
    ({**ANSWER, "custom": [{"token": "sk-live-123"}]}, "must be secret, public"),
    ({**ANSWER, "entries": [{"id": "e1"}]}, "expected shipped and custom"),
    ({**ANSWER, "shipped": [{**ANSWER["shipped"][0], "api_key": "sk-live-123"}]}, "exactly kind, service"),
    ({**ANSWER, "shipped": [{**ANSWER["shipped"][0], "auth_types": ["sk live 123"]}]}, "list of ids"),
    ({**ANSWER, "custom": [{"token": "secret"}, {"token": "secret"}]}, "listed twice"),
    ({**ANSWER, "custom": [{"url": "public"}]}, "at least one secret"),
])
def test_an_answer_carrying_anything_else_is_refused(answer, message):
    with pytest.raises(ValueError, match=message):
        structures_from_answer(answer)


def test_gen_keystore_structures_over_the_test_bridge(native_bridge):
    """The testing bridge answers the distinct structures of its custom
    accounts, as the host answers the user's: no values, no names, no count."""
    import gen
    native_bridge._rec_accounts.update({
        "a": {"kind": "custom", "name": "Mine", "metadata": {"structure": WANTED, "values": {"url": "https://issues.example"}},
              "secrets": {"auth.token": "s3cret-value"}},
        "b": {"kind": "custom", "name": "Another", "metadata": {"structure": dict(reversed(list(WANTED.items())))},
              "secrets": {"auth.token": "other-value"}},
        "m": {"kind": "model", "name": "Model", "metadata": {}, "secrets": {"api_key": "sk-live"}},
    })
    structures = gen.keystore_structures()
    assert isinstance(structures, KeystoreStructures)
    assert [item.as_manifest() for item in structures.custom] == [check_structure(WANTED)]
    text = json.dumps([str(v) for v in _values(structures)])
    for leaked in ("https://issues.example", "s3cret-value", "other-value", "sk-live", "Mine", "Another"):
        assert leaked not in text and leaked not in repr(structures)


def test_lint_refuses_a_custom_role_without_a_secret_and_a_structure_elsewhere(tmp_path):
    from collimate.lint import account_structure_problem
    assert account_structure_problem({"kind": "custom", "structure": WANTED}) is None
    assert "at least one secret key" in account_structure_problem({"kind": "custom", "structure": {"url": "public"}})
    assert "declares the structure it reads" in account_structure_problem({"kind": "custom"})
    assert "custom alone" in account_structure_problem({"kind": "account", "structure": WANTED})
    assert "custom alone" in account_structure_problem({"kinds": ["custom", "account"], "structure": WANTED})
    assert account_structure_problem({"kind": "model"}) is None


def test_collimate_lint_reports_the_structure_rule_on_a_generator(tmp_path):
    from collimate.lint import lint_generator_manifest
    folder = tmp_path / "tracker"
    folder.mkdir()
    (folder / "main.py").write_text("import gen\n")
    (folder / "settings.set_yaml").write_text("fields: []\n")

    def errors(structure):
        (folder / "tracker.generator").write_text(
            "id: tracker\nname: Tracker\nsummary: Files issues\nentrypoint: main.py\nmain_config: settings.set_yaml\n"
            "account_requirements:\n  tracker:\n    kind: custom\n" + structure)
        return [i.code for i in lint_generator_manifest(folder / "tracker.generator") if i.level.name == "ERROR"]
    assert "account-structure" in errors("    structure:\n      url: public\n")
    assert "account-structure" in errors("")
    assert "account-structure" not in errors("    structure:\n      url: public\n      auth:\n        token: secret\n")
