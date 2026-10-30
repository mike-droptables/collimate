"""Terminal resize value contracts at the shared transport owner."""

import pytest

from terminal_dimensions import clamp_dim


@pytest.mark.parametrize("value,expected", [
    (None, 24), ("", 24), ("bad", 24), ([], 24), ({}, 24),
    (float("inf"), 24), (float("nan"), 24),
    (0, 24), (-1, 24), (False, 24),
    (True, 1), (1, 1), ("80", 80), (12.9, 12),
    (1000, 1000), (1001, 1000), (10**30, 1000),
])
def test_resize_dimensions_preserve_conversion_fallback_and_cap__P6F01(value, expected):
    assert clamp_dim(value, 24) == expected


def test_resize_rows_and_columns_keep_distinct_defaults__P6F01():
    assert (clamp_dim(None, 24), clamp_dim(None, 80)) == (24, 80)


def test_resize_conversion_precedes_positive_bound_check__P6F01():
    conversions = []

    class Dimension:
        def __int__(self):
            conversions.append("int")
            return 1001

    assert clamp_dim(Dimension(), 80) == 1000
    assert conversions == ["int"]


def test_terminal_route_uses_shared_dimension_owner__P6F01():
    from routes import terminal

    assert terminal.clamp_dim is clamp_dim
    assert "_clamp_dim" not in vars(terminal)
