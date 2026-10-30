"""Shared real/recording bridge wire facts, characterized before ownership move."""
from collimate._host.bridge import PartialRefusal
from collimate._host.ipc import DEV_CLIENT_CAPS, refused_message
from collimate.testing import make_test_bridge


def test_dev_capabilities_are_isolated_between_recording_bridges_P6F01(tmp_path):
    first = make_test_bridge(tmp_path / 'first')
    second = make_test_bridge(tmp_path / 'second')
    first.stub_client_caps({'client': 'electron', 'native_guests': True})
    observed = first.client_caps()
    observed['external_open'] = True
    assert first.client_caps()['external_open'] is False
    assert second.client_caps() == DEV_CLIENT_CAPS
    assert second.client_caps()['client'] == 'dev'
    assert all(value is False for key, value in second.client_caps().items() if key != 'client')


def test_refusal_wording_and_error_map_share_exact_wire_reasons_P6F01():
    refused = [
        {'path': 'invalid.checked', 'reason': 'handler_invalid', 'error': 'needs good'},
        {'path': 'card.run', 'reason': 'host_owned'},
        {'path': 'ui.txt', 'reason': 'ui_type'},
        {'path': 'locked.txt', 'reason': 'locked', 'owner': 'other-run'},
    ]
    text = refused_message(refused)
    assert text == ('invalid.checked: needs good; card.run is the run card: the host owns it; '
                    'ui.txt is not a UI file type the manifest declares in ui_files; '
                    'locked.txt is locked by other-run (the rest of the update was committed)')
    error = PartialRefusal(text, paths=[entry['path'] for entry in refused], refused=refused)
    assert str(error) == text
    assert error.errors['invalid.checked'] == 'needs good'
    assert error.errors['locked.txt'] == refused_message([refused[-1]])
    assert error.paths == {entry['path'] for entry in refused}


def test_wire_facts_have_one_public_leaf_owner_P6F01():
    from collimate._host import bridge as _host
    from collimate.testing import _bridge

    assert refused_message.__module__ == "collimate._host.ipc"
    assert _bridge.ipc is _host.ipc
    assert not hasattr(_host, "_DEV_CLIENT_CAPS")
    assert not hasattr(_host, "_refused_message")
