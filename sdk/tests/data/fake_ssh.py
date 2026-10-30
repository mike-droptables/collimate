# SPDX-License-Identifier: MIT
"""A stand-in for ``ssh`` in the git engine's tests (the ``fake_ssh`` fixture).

git runs it as ``ssh [options] [-p PORT] user@host '<cmd> '\''<path>'\'''``.
It appends one JSON line per call to ``$FAKE_SSH_LOG`` (argv, user, host,
port, cmd, the exact path git sent, the key file's and its folder's modes,
the key's sha256, the known_hosts lines and the git/ssh/token variable names
in its environment), refuses a host its ``UserKnownHostsFile`` does not name
as ssh does ("Host key verification failed.", exit 255), and otherwise runs
git's upload-pack or receive-pack on ``$FAKE_SSH_ROOT/<host>[_<port>]/<path>``
(the path without a leading ``/``, ``~/`` or ``~user/``). With
``$FAKE_SSH_BLOCK`` set to a path it waits after logging until that file
exists, or until git closes its end (git was killed). ``$FAKE_SSH_REFUSE``
set to ``host_key`` or ``publickey`` answers as ssh does when the server's
host key changed or the server refuses the key (exit 255). Stdlib only."""
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import stat
import sys

_WITH_VALUE = set("bcDEeFIiJLlmOoPpQRSWw")


def _host_key_name(host: str, port) -> str:
    return host if port in (None, 22) else f"[{host}]:{port}"


def main(argv: list[str]) -> int:
    options: dict[str, str] = {}
    key = port = None
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        flag = argv[i][1:]
        if flag and flag[0] in _WITH_VALUE:
            value = flag[1:] or argv[i + 1]
            i += 1 if flag[1:] else 2
            if flag[0] == "o":
                name, _, setting = value.partition("=")
                options[name.lower()] = setting
            elif flag[0] == "i":
                key = value
            elif flag[0] == "p":
                port = int(value)
            continue
        i += 1
    destination, command = argv[i], " ".join(argv[i + 1:])
    user, _, host = destination.rpartition("@")
    words = shlex.split(command)
    cmd, path = words[0], words[1]
    record = {"argv": argv, "user": user, "host": host, "port": port, "cmd": cmd, "path": path,
              "key_mode": None, "dir_mode": None, "dir_owner_is_uid": None, "key_bytes_sha256": None,
              "known_hosts": [],
              "env_has": sorted(k for k in os.environ
                                if k.startswith(("GIT_", "SSH_", "GH_", "GITHUB_", "GITLAB_")))}
    if key:
        info, folder = os.stat(key), os.stat(os.path.dirname(key))
        with open(key, "rb") as fh:
            record["key_bytes_sha256"] = hashlib.sha256(fh.read()).hexdigest()
        record.update(key_mode=stat.S_IMODE(info.st_mode), dir_mode=stat.S_IMODE(folder.st_mode),
                      dir_owner_is_uid=folder.st_uid == os.getuid())
    known_file = options.get("userknownhostsfile")
    if known_file and os.path.exists(known_file):
        with open(known_file, encoding="utf-8") as fh:
            record["known_hosts"] = [line for line in fh.read().splitlines() if line]
    with open(os.environ["FAKE_SSH_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    block = os.environ.get("FAKE_SSH_BLOCK")
    while block and not os.path.exists(block):
        # git speaks only after the server, so readable stdin here is git's end closing.
        if select.select([0], [], [], 0.05)[0] and not os.read(0, 1):
            return 255
    refuse = os.environ.get("FAKE_SSH_REFUSE")
    if refuse == "host_key":
        sys.stderr.write("@@@@@@@@@@@\n@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
                         "Host key verification failed.\n")
        return 255
    if refuse == "publickey":
        sys.stderr.write(f"{destination}: Permission denied (publickey).\n")
        return 255
    names = {name for line in record["known_hosts"] for name in line.split(" ", 1)[0].split(",")}
    if _host_key_name(host, port) not in names:
        sys.stderr.write("Host key verification failed.\n")
        return 255
    relative = re.sub(r"^(/|~[A-Za-z0-9_.-]*/)", "", path).removesuffix(".git")
    where = host if port is None else f"{host}_{port}"
    repository = os.path.join(os.environ["FAKE_SSH_ROOT"], where.lower(), f"{relative.lower()}.git")
    if cmd not in ("git-upload-pack", "git-receive-pack") or not os.path.isdir(repository):
        sys.stderr.write("fatal: '" + path + "' does not appear to be a git repository\n")
        return 128
    git = shutil.which("git")
    os.execv(git, [git, cmd.removeprefix("git-"), repository])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
