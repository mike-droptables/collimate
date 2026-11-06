# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""A generator with no third-party dependency, so a run of it needs no
network: it writes ``ran-with.json``, naming the uv that started it (uv
names itself to the processes it starts as ``$UV``), the Python it got (its
version and home, ``sys.base_prefix``) and what its PYTHONPATH holds.

build/check_server_env.py and frontend/tests/electron/packaged.spec.ts add
it to a channel and run it, to prove a package's server starts generator
runs with the uv the package carries, on the Python it carries, and with
nothing of the server's on their path but the SDK.
"""
import json
import os
import platform
import sys

import gen


async def run():
    gen.update_file("ran-with.json", json.dumps({
        "uv": os.environ.get("UV"),
        "python": platform.python_version(),
        "python_home": os.path.realpath(sys.base_prefix),
        "pythonpath": [{"path": entry, "holds": sorted(os.listdir(entry))}
                       for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep) if entry],
    }) + "\n")


if __name__ == "__main__":
    gen.main(run)
