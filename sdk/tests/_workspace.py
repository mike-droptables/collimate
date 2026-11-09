# SPDX-License-Identifier: MIT
"""The workspace checkout the SDK's tests exercise: this member (``sdk/``) and
the first-party trees beside it at the workspace root."""
from pathlib import Path

SDK = Path(__file__).resolve().parents[1]
WORKSPACE = SDK.parent
CONTENT = WORKSPACE / "content"
GENERATORS = CONTENT / "generators"
RENDERERS = CONTENT / "renderers"
SUGGESTED = CONTENT / "suggested"
BUILTINS = WORKSPACE / "builtins"
MODULE_SNAPSHOT = WORKSPACE / "module-snapshot"
# The host's base image (``collimate-linux-small``): its Dockerfile and the
# module that stages the SDK into its build context.
HOST_BASE_IMAGE = WORKSPACE / "api" / "collimate_server" / "process_manager" / "base_image.py"


def host_base_image():
    """The host's base-image module, loaded on its own from its file: it
    imports only the stdlib and the SDK, never the rest of the server."""
    import importlib.util
    import sys
    name = "collimate_host_base_image"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HOST_BASE_IMAGE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
