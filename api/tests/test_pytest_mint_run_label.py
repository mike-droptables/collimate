"""The public host minter has an explicit verb."""


def test_host_exports_only_the_explicit_run_label_minter__F1884():
    import run_folders
    from collimate.pointers import LABEL_RE

    assert hasattr(run_folders, "mint_run_label")
    assert "mint_run_label" in run_folders.__all__
    assert not hasattr(run_folders, "run_label")
    assert "run_label" not in run_folders.__all__
    run_id = "3f9a2c1d-0000-4000-8000-000000000000"
    for runner_id, expected in [("test-runner", "test-runner-3f9a2c1d"), (None, "run-3f9a2c1d")]:
        label = run_folders.mint_run_label(runner_id, run_id)
        assert label == expected
        assert LABEL_RE.fullmatch(label)
        assert run_folders.run_folder(label) == f"_runs/{expected}"
        assert run_folders.run_card_path(label) == f"_runs/{expected}/{expected}.run"
