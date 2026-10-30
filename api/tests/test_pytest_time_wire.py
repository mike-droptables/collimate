"""O03: one timestamp shape on the wire, from the channel's three conventions."""
from datetime import datetime, timedelta, timezone

import pytest

from time_wire import iso, now_iso


def test_sqlite_current_timestamp_is_utc_O03():
    assert iso("2026-10-06 14:05:00") == "2026-10-06T14:05:00+00:00"
    assert iso("2026-10-06 14:05:00.987") == "2026-10-06T14:05:00+00:00"


def test_naive_layout_datetime_is_local_time_O03():
    local = datetime(2026, 10, 6, 9, 5, 0, 123456)
    expected = local.astimezone().astimezone(timezone.utc).replace(microsecond=0).isoformat()
    assert iso(local) == expected
    assert iso(local).endswith("+00:00")


def test_aware_datetime_is_converted_to_utc_O03():
    plus_two = timezone(timedelta(hours=2))
    assert iso(datetime(2026, 10, 6, 16, 5, 0, tzinfo=plus_two)) == "2026-10-06T14:05:00+00:00"


def test_iso_strings_with_an_offset_are_converted_O03():
    assert iso("2026-10-06T14:05:00.5+00:00") == "2026-10-06T14:05:00+00:00"
    assert iso("2026-10-06T16:05:00+02:00") == "2026-10-06T14:05:00+00:00"
    assert iso("2026-10-06T14:05:00Z") == "2026-10-06T14:05:00+00:00"


def test_none_passes_and_ambiguous_values_are_refused_O03():
    assert iso(None) is None
    with pytest.raises(ValueError):
        iso("2026-10-06T14:05:00")  # a naive layout dump: local or UTC?
    with pytest.raises(ValueError):
        iso("yesterday")
    with pytest.raises(TypeError):
        iso(1700000000)  # type: ignore[arg-type]


def test_now_is_whole_seconds_utc_and_sorts_as_text_O03():
    first = now_iso()
    assert first.endswith("+00:00") and "." not in first
    assert iso(first) == first
    assert sorted([iso("2026-10-06 14:05:01"), iso("2026-10-06T14:05:00+00:00")])[0] == "2026-10-06T14:05:00+00:00"
