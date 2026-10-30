"""Host fixture storage stays inside each test's owned scratch directory."""

from pathlib import Path
from types import SimpleNamespace


def test_docker_fixture_owns_home_scratch_and_cleans_it__F1909(tmp_path, monkeypatch):
    import sandbox
    from test_pytest_docker_runner import _home_app

    monkeypatch.setenv("HOME", str(tmp_path))
    personal = tmp_path / ".collimate"
    personal.mkdir()
    sentinel = personal / "personal-data"
    sentinel.write_text("keep")
    monkeypatch.setattr(sandbox, "reap_orphan_containers", lambda: None)
    with _home_app():
        workdir = Path.cwd()
        assert workdir.is_relative_to(tmp_path)
        assert not workdir.is_relative_to(personal)
        import accounts

        assert accounts._cli_root().is_relative_to(workdir)
    assert not workdir.exists()
    assert sentinel.read_text() == "keep"


def test_app_fixture_materializes_accounts_only_in_its_scratch__P0F30(
    client, fresh_cwd, monkeypatch
):
    import accounts

    runtime = accounts._cli_root()
    assert runtime.is_relative_to(fresh_cwd), (
        f"account runtime escaped fixture: {runtime}"
    )
    monkeypatch.setattr(
        accounts,
        "PLAN_LOGINS",
        {
            "fixture": SimpleNamespace(lease=("login.json",), field="credential_json"),
        },
    )
    monkeypatch.setattr(accounts, "_read", lambda: {"accounts": {}})
    lease = accounts.materialize_cli_account(
        {
            "metadata": {"harness": "fixture"},
            "credential_account_id": "fixture-account",
            "credential_generation": "fixture-generation",
            "secrets": {"credential_json": '{"fixture":true}'},
        },
        "fixture-owner",
    )
    try:
        directory = Path(lease["path"])
        assert directory.parent == runtime
        assert (directory / "fixture" / "login.json").read_text() == '{"fixture":true}'
    finally:
        accounts._OWNED_CLI_LEASES.pop(lease["lease_id"])
