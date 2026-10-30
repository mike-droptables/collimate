"""Cell route parameter names follow one vocabulary."""

import inspect

import pytest


@pytest.mark.parametrize("endpoint_name,method,suffix", [
    ("get_cell_history", "GET", "history"),
    ("get_cell_files_tree", "GET", "files-tree"),
    ("fork_version", "POST", "fork"),
])
def test_cell_routes_use_one_path_and_parameter_name__F0287(endpoint_name, method, suffix):
    from routes import cells

    endpoint = getattr(cells, endpoint_name)
    route = next(route for route in cells.router.routes if route.endpoint is endpoint)
    assert route.path == f"/api/channels/{{channel}}/cells/{{cell_id}}/{suffix}"
    assert method in route.methods
    parameters = inspect.signature(endpoint).parameters
    assert "cell_id" in parameters
    assert "visible_cell_id" not in parameters
    assert "internal_cell_id" not in parameters
    assert {field.name for field in route.dependant.path_params} == {"channel", "cell_id"}
    if endpoint_name == "fork_version":
        assert set(cells.ForkRequest.model_fields) == {"target_cell_id"}
