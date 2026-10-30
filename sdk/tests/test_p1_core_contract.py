"""The cell-local generator contract."""
import inspect
from pathlib import Path

import gen
from collimate import native, pointers
from collimate.lint.core import _Ctx, _check_runner_permissions, Level
from collimate.permissions import AXES


def test_cell_local_public_surface__D01_D02_F1226_F2029_F2020_F1129_F1170_O08():
    assert set(gen.__all__) == set('''Annotation AnnotationNotice ConfigField ConfigValidation ContextItem DraftFileSchema Endpoint
        GeneratorResult Grant Message RunInfo HandlerInfo HandlerType ValidationResult place_environment exec_environment
        expose_environment_port close_environment grant run_info run_path packages handlers handler_for handler_skills validate
        account commit_account materialize_account release_account client_caps Cancelled
        FileConflictError IPCDisconnectedError IPCError IPCNakError IPCTimeoutError PartialRefusal config config_folder
        config_folder_path config_folders options delete_file
        has_file list_files read_file track_file update_file update_ui_file delete_ui_file list_ui_files
        require_ui_types reveal_file commit annotations ChatTurn history serve_chat ask can_ask
        finish log phase progress actions proxy_url
        serve_port serve_terminal snapshot start_terminal stop_terminal terminal_status wait_terminal_exit
        wait_for_action wait_for_action_event wait_until_cancelled workspace_path live_files main'''.split())


def test_chat_pointer_and_signature__F1233_F1232_F1166_F1167_F1744():
    assert set(inspect.signature(native.serve_chat).parameters) == {
        'on_turn', 'label', 'threads_dir', 'pointer_file', 'initial_message',
        'on_ready', 'files', 'broadcast',
    }
    assert set(pointers.chat_session_pointer({}, threads_dir='chat')) == {
        'chat_id', 'ws_path', 'token', 'label', 'threads_dir', 'active_thread', 'threads',
    }


def test_runner_names_axes_to_delete__D03():
    ctx = _Ctx(Path('sample.runner'), {})
    _check_runner_permissions({'side_cells': True, 'authoring': 'run', 'renderers': 'read'}, ctx)
    errors = [i.message for i in ctx.issues if i.level == Level.ERROR]
    assert len(errors) == 1
    assert errors[0] == 'Delete removed permission axes: authoring, renderers, side_cells'
    assert set(AXES) == {'update_packages', 'docker', 'internet', 'internet_allow', 'commands'}


def test_pointer_key_sets_match_written_shapes__F1232(native_bridge):
    assert set(pointers.CHAT_KEYS) == set(pointers.chat_session_pointer({}, threads_dir="chat"))
    assert set(pointers.WEBSITE_KEYS) == set(pointers.website_pointer(8080))
