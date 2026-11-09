# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic==2.12.5"]
# ///
"""A generator whose header needs a third-party dependency with a compiled
extension that the server environment carries too, at another version:
pydantic (and its pydantic_core) at this pin, which uv fetches on the first
run and caches. It writes ``own-deps.json``: the same facts as the run probe
(``build/run_probe/``: the uv, the Python and its home, the PYTHONPATH) and
where pydantic and pydantic_core were imported from, at which versions.

build/check_server_env.py and frontend/tests/electron/packaged.spec.ts run
it to prove a run imports its own dependencies, not the server's, on the
Python the package carries.
"""
import json
import os
import platform
import sys

import pydantic
import pydantic_core

import gen


async def run():
    gen.update_file("own-deps.json", json.dumps({
        "uv": os.environ.get("UV"),
        "python": platform.python_version(),
        "python_home": os.path.realpath(sys.base_prefix),
        "pythonpath": [{"path": entry, "holds": sorted(os.listdir(entry))}
                       for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep) if entry],
        "pydantic": {"version": pydantic.VERSION, "file": os.path.realpath(pydantic.__file__)},
        "pydantic_core": {"version": pydantic_core.__version__, "file": os.path.realpath(pydantic_core.__file__)},
    }) + "\n")


if __name__ == "__main__":
    gen.main(run)
