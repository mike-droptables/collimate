import pytest


def manifest(**changes):
    return dict(id="document", name="Document", description="A document", types=[{"ext": ".doc"}],
                guidance="Use a title", examples=[], **changes)


def test_derived_identity_owns_exact_bytes_and_paths__D09_D17():
    from collimate.handlers import derived_hash
    files = {"verify.ts": b"export default () => ({valid:true})", "skills/doc/SKILL.md": b"# Guide\r\n"}
    assert derived_hash(files) == derived_hash(dict(reversed(list(files.items()))))
    assert derived_hash(files) != derived_hash({**files, "skills/doc/SKILL.md": b"# Guide\n"})
    for path in ("../verify.ts", "/verify.ts", "a//b", "_runs/x", "a/../x", "a\\b"):
        with pytest.raises(ValueError):
            derived_hash({path: b"x"})


def test_handler_schema_refuses_reserved_and_conflicting_claims__D09():
    from collimate.handlers import validate_handler
    data = manifest()
    data["types"] = [{"ext": ".Renderer"}]
    with pytest.raises(ValueError, match="reserved"):
        validate_handler(data)
    assert validate_handler(data, allow_reserved=True)["types"][0]["ext"] == ".renderer"
    data["types"] = [{"ext": ".doc"}, {"ext": ".DOC"}]
    with pytest.raises(ValueError, match="duplicate"):
        validate_handler(data)
    for retired in ({"ext": ".doc", "surface": True}, {"ext": ".doc", "comments" + "_for": ".x"}):
        data["types"] = [retired]
        with pytest.raises(ValueError, match="Unknown handler type field"):
            validate_handler(data)


def test_handler_matching_is_casefolded_longest_and_does_not_chain_aliases__D09_D11():
    from collimate.handlers import handler_for
    short = {"id": "short", "types": [{"ext": ".html"}]}
    long = {"id": "long", "types": [{"ext": ".notes.html", "render_as": ".html"}]}
    assert handler_for([short, long], "Report.NOTES.HTML") is long
    assert handler_for([short, long], ".HTML") is short
    assert handler_for([short, long], "nothing.txt") is None
    assert handler_for([short, {"id": "collision", "types": [{"ext": ".HTML"}]}], ".html") is None
    assert handler_for([short, long, {**long, "id": "collision"}], "report.notes.html") is None


def test_handler_schema_rejects_unknown_and_malformed_fields__D09_D12():
    from collimate.handlers import validate_handler
    for changes in ({"unknown": True}, {"derived": "unaccepted"}, {"types": []},
                    {"id": "../id"}, {"examples": [{"folder": "../escape", "expect": "valid"}]}):
        with pytest.raises(ValueError):
            validate_handler({**manifest(), **changes})
    value = validate_handler(manifest())
    assert value["types"] == [{"ext": ".doc", "render_as": None}]
