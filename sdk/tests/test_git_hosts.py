# SPDX-License-Identifier: MIT
"""``collimate.git_hosts`` — the pure Git account checks the Key manager's Save
and Verify and the runs share: the pinned github.com and gitlab.com host keys,
known_hosts and OpenSSH private-key parsing, and the account address rules."""
import ast
import base64
import struct
from pathlib import Path

import pytest

from collimate import git_hosts
from collimate.git_hosts import KnownHost


def _string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def _container(keytype: str, *, cipher: str = "none", kdf: str = "none", nkeys: int = 1,
               checks: tuple = (7, 7), private_type: "str | None" = None) -> str:
    """A synthetic ``openssh-key-v1`` private key whose public blob names ``keytype``
    (ssh-keygen 10 makes neither DSA nor, without hardware, security keys)."""
    public = _string(keytype.encode()) + _string(b"\x01" * 32)
    private = struct.pack(">II", *checks) + _string((private_type or keytype).encode()) + b"\x00" * 40
    data = (b"openssh-key-v1\0" + _string(cipher.encode()) + _string(kdf.encode()) + _string(b"")
            + struct.pack(">I", nkeys) + _string(public) + _string(private))
    body = base64.b64encode(data).decode()
    lines = [body[i:i + 70] for i in range(0, len(body), 70)]
    return "\n".join(["-----BEGIN OPENSSH PRIVATE KEY-----", *lines, "-----END OPENSSH PRIVATE KEY-----"]) + "\n"


def _refusal(fn, *args) -> str:
    with pytest.raises(ValueError) as exc:
        fn(*args)
    return str(exc.value)


def test_pinned_lines_match_the_published_fingerprints():
    assert set(git_hosts.PINNED_HOST_KEYS) == {"github.com", "gitlab.com"} == set(git_hosts.PINNED_FINGERPRINTS)
    for host, lines in git_hosts.PINNED_HOST_KEYS.items():
        entries = git_hosts.parse_known_hosts("\n".join(lines))
        assert [e.keytype for e in entries] == ["ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"]
        assert all(e.hosts == (host,) for e in entries)
        assert {git_hosts.fingerprint(e.blob) for e in entries} == git_hosts.PINNED_FINGERPRINTS[host]
        assert [e.line() for e in entries] == list(lines)
    pinned = git_hosts.pinned_known_hosts()
    assert len(pinned) == 6 and {h for e in pinned for h in e.hosts} == {"github.com", "gitlab.com"}


def test_parse_known_hosts_accepts_plain_port_and_comma_lists(ssh_keys):
    ed25519_blob = ssh_keys.blob()
    text = ("# from ssh-keyscan\r\n\n"
            f"Box.Example.org ssh-ed25519 {ed25519_blob} a comment\r\n"
            f"[box.example.org]:2222,10.0.0.7 ssh-ed25519 {ed25519_blob}\n"
            f"[box.example.org]:22 ssh-ed25519 {ed25519_blob}\n")
    entries = git_hosts.parse_known_hosts(text)
    assert entries == (
        KnownHost(("box.example.org",), "ssh-ed25519", ed25519_blob),
        KnownHost(("[box.example.org]:2222", "10.0.0.7"), "ssh-ed25519", ed25519_blob),
        KnownHost(("box.example.org",), "ssh-ed25519", ed25519_blob),
    )
    assert entries[1].line() == f"[box.example.org]:2222,10.0.0.7 ssh-ed25519 {ed25519_blob}"
    assert git_hosts.parse_known_hosts("") == ()
    assert git_hosts.host_key_name("Box.example.org", None) == "box.example.org"
    assert git_hosts.host_key_name("box.example.org", 22) == "box.example.org"
    assert git_hosts.host_key_name("box.example.org", 2222) == "[box.example.org]:2222"


def test_parse_known_hosts_refusals(ssh_keys):
    ed25519_blob = ssh_keys.blob()
    rsa_blob = ssh_keys.blob("rsa")
    cases = {
        "box.example.org ssh-ed25519": "Known hosts line 1 is not a known_hosts line (host keytype base64); "
                                       "paste the output of ssh-keyscan",
        f"-bad.example ssh-ed25519 {ed25519_blob}": "Known hosts line 1 is not a known_hosts line",
        f"2001:db8::1 ssh-ed25519 {ed25519_blob}": "Known hosts line 1 is not a known_hosts line",
        f"[box]:70000 ssh-ed25519 {ed25519_blob}": "Known hosts line 1 is not a known_hosts line",
        f"@other box ssh-ed25519 {ed25519_blob}": "Known hosts line 1 is not a known_hosts line",
        f"|1|abc=|def= ssh-ed25519 {ed25519_blob}": "Known hosts line 1 is hashed; paste the unhashed line "
                                                    "(ssh-keyscan without -H)",
        f"*.example.org ssh-ed25519 {ed25519_blob}": "Known hosts line 1 uses a wildcard; pin each host by name",
        f"box,!evil ssh-ed25519 {ed25519_blob}": "Known hosts line 1 uses a wildcard",
        f"@cert-authority *.example.org ssh-ed25519 {ed25519_blob}":
            "Known hosts line 1 uses @cert-authority, which runs do not support",
        f"@revoked box ssh-ed25519 {ed25519_blob}": "Known hosts line 1 uses @revoked, which runs do not support",
        "box ssh-dss AAAAB3NzaC1kc3M=": "Known hosts line 1 has an unsupported key type ssh-dss",
        "box sk-ssh-ed25519@openssh.com AAAA": "Known hosts line 1 has an unsupported key type "
                                               "sk-ssh-ed25519@openssh.com",
        "box ssh-ed25519 not-base64!": "Known hosts line 1 has a key that does not decode as ssh-ed25519",
        f"box ssh-ed25519 {rsa_blob}": "Known hosts line 1 has a key that does not decode as ssh-ed25519",
    }
    for text, message in cases.items():
        assert _refusal(git_hosts.parse_known_hosts, text).startswith(message), text
    # The line number counts every line of the paste, comments and blanks included.
    assert _refusal(git_hosts.parse_known_hosts, f"# c\n\nbox ssh-ed25519 {ed25519_blob}\nbox x") \
        .startswith("Known hosts line 4 ")
    # A token pasted into the key type's place is never echoed.
    message = _refusal(git_hosts.parse_known_hosts, "host ghp_SECRETTOKEN AAAA")
    assert message == "Known hosts line 1 has an unsupported key type"
    assert "ghp_SECRETTOKEN" not in message
    line = f"box ssh-ed25519 {ed25519_blob}"
    assert len(git_hosts.parse_known_hosts("\n".join([line] * 64))) == 64
    for text in ("\n".join([line] * 65), "#" * (16 * 1024 + 1)):
        assert _refusal(git_hosts.parse_known_hosts, text) == "Known hosts holds more than 64 lines or 16 KiB"


def test_inspect_private_key_accepts_ed25519_ecdsa_rsa_and_matches_ssh_keygen_fingerprints(ssh_keys):
    expected = ssh_keys.fingerprints
    assert set(expected) == {"ed25519", "ecdsa", "rsa"}
    for name, facts in expected.items():
        info = git_hosts.inspect_private_key(ssh_keys[name])
        assert info.keytype == facts["type"]
        assert info.fingerprint == facts["fingerprint"]
        assert info.public_key == ssh_keys.public(name)
        assert git_hosts.fingerprint(info.public_key.split()[1]) == facts["fingerprint"]
    # Pasting picks up CRLF line ends and surrounding blank lines.
    crlf = "\n\n" + ssh_keys["ed25519"].replace("\n", "\r\n") + "  \n"
    assert git_hosts.inspect_private_key(crlf).fingerprint == expected["ed25519"]["fingerprint"]


def test_inspect_private_key_refusals_in_order(ssh_keys):
    unreadable = ("The SSH key could not be read; paste the whole private key file, from "
                  "-----BEGIN OPENSSH PRIVATE KEY----- to -----END OPENSSH PRIVATE KEY-----")
    passphrase = ("This SSH key is protected by a passphrase, which Collimate cannot use. Paste an unencrypted "
                  "copy (ssh-keygen -p -N \"\" -f <copy>) or a dedicated deploy key without a passphrase")
    security = ("This is a security-key (FIDO) SSH key, which needs its hardware key touched at every use; runs "
                "cannot do that. Create an Ed25519 key without a passphrase (ssh-keygen -t ed25519 -N \"\") for "
                "Collimate")
    unknown = ("DSA and unknown key types are refused by GitHub and GitLab; create an Ed25519 key "
               "(ssh-keygen -t ed25519)")
    good = ssh_keys["ed25519"]
    lines = good.splitlines()
    cases = [
        (ssh_keys["ed25519.pub"], "That is a public key; paste the private key (the file without .pub)"),
        ("sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29t comment",
         "That is a public key; paste the private key (the file without .pub)"),
        (ssh_keys["ed25519_putty.txt"],
         "This is a PuTTY key; export it from PuTTYgen with Conversions › Export OpenSSH key"),
        (ssh_keys["rsa_pem"], "This is an old-format key; rewrite a copy in OpenSSH format with ssh-keygen -p -N \"\" "
                          "-f <copy> and paste that"),
        ("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----", "This is an old-format key"),
        ("-----BEGIN ENCRYPTED PRIVATE KEY-----\nAAAA\n-----END ENCRYPTED PRIVATE KEY-----",
         "This is an old-format key"),
        ("not a key", unreadable),
        ("", unreadable),
        (good + good, unreadable),                                   # two blocks
        ("\n".join(lines[:-1]), unreadable),                         # no END line
        ("\n".join(lines[:2] + lines[-1:]), unreadable),             # truncated body
        ("\n".join([lines[0], "AAAA$AAA", lines[-1]]), unreadable),  # not base64
        ("\n".join([lines[0], *["A" * 70] * 240, lines[-1]]), unreadable),   # over 16 KiB
        ("\n".join([lines[0], base64.b64encode(b"openssh-key-v2\0" + b"\0" * 32).decode(), lines[-1]]),
         unreadable),                                                # no magic
        (_container("ssh-ed25519", nkeys=2), unreadable),
        (ssh_keys["ed25519_encrypted"], passphrase),
        (_container("ssh-ed25519", kdf="bcrypt"), passphrase),
        (_container("sk-ssh-ed25519@openssh.com"), security),
        (_container("sk-ecdsa-sha2-nistp256@openssh.com"), security),
        (_container("ssh-dss"), unknown),
        (_container("ssh-unknown"), unknown),
        (_container("ssh-ed25519", checks=(1, 2)), unreadable),
        (_container("ssh-ed25519", private_type="ssh-rsa"), unreadable),
    ]
    for text, message in cases:
        assert _refusal(git_hosts.inspect_private_key, text).startswith(message), text[:60]
    # The synthetic container itself is well formed: with a supported type and matching check-ints it is accepted.
    assert git_hosts.inspect_private_key(_container("ssh-ed25519")).keytype == "ED25519"
    # No refusal echoes the key's text.
    secret = lines[1]
    for text, _ in cases:
        try:
            git_hosts.inspect_private_key(text)
        except ValueError as exc:
            assert secret not in str(exc)


def test_normalise_private_key(ssh_keys):
    good = ssh_keys["ed25519"]
    assert git_hosts.normalise_private_key(good) == good
    assert git_hosts.normalise_private_key(good.rstrip("\n")) == good
    assert git_hosts.normalise_private_key("\r\n  " + good.replace("\n", "\r\n") + "\r\n\r\n") == good
    assert git_hosts.normalise_private_key(good.replace("\n", "\r")) == good
    assert git_hosts.normalise_private_key("") == "\n"


def test_git_base_and_endpoint_problem():
    e_a = "A Git account's address must be an https URL"
    e_b = "A Git account's address has no user name, password, query or fragment"
    e_c = ("A GitHub account's address is where its repositories open in a browser (https://github.com, or your "
           "GitHub Enterprise server), not its API URL")
    e_d = ("A GitLab account's address is the instance's address (https://gitlab.com, or https://host[/path]), "
           "not its /api/v4")
    e_e = "A Git server account with an SSH key or no credentials has no server URL"
    assert git_hosts.git_base("HTTPS://GHE.Example.com/") == "https://ghe.example.com"
    assert git_hosts.git_base("https://ghe.example.com:443/") == "https://ghe.example.com"
    assert git_hosts.git_base("https://example.com:8443/GitLab/") == "https://example.com:8443/GitLab"
    assert git_hosts.git_base(" https://git.example.org ") == "https://git.example.org"
    for bad in ("http://github.com", "github.com", "ssh://git@github.com", "https://", "https://exa mple.com",
                "https://-bad.example", "https://example.com:0", "https://example.com:99999",
                "https://example.com/a/../b", "https://example.com/a b", "https://[::1]/"):
        assert _refusal(git_hosts.git_base, bad) == e_a, bad
    for bad in ("https://user@example.com", "https://u:p@example.com", "https://example.com/?x=1",
                "https://example.com/?", "https://example.com/#top"):
        assert _refusal(git_hosts.git_base, bad) == e_b, bad

    problem = git_hosts.endpoint_problem
    assert problem("github", "api_key", "") is None
    assert problem("github", "api_key", "https://github.com") is None
    assert problem("github", "ssh_key", "https://ghe.example.com") is None
    assert problem("github", "api_key", "http://github.com") == e_a
    assert problem("gitlab", "api_key", "https://u:p@gitlab.com") == e_b
    for api in ("https://api.github.com", "https://API.github.com/", "https://api.octo.ghe.com",
                "https://ghe.example.com/api/v3", "https://ghe.example.com/api/v3/"):
        assert problem("github", "api_key", api) == e_c, api
    assert problem("gitlab", "api_key", "https://gitlab.com/api/v4") == e_d
    assert problem("gitlab", "api_key", "https://example.com/gitlab/api/v4/") == e_d
    assert problem("gitlab", "ssh_key", "https://example.com/gitlab") is None
    assert problem("github", "api_key", "https://gitlab.com/api/v4") is None
    assert problem("git", "ssh_key", "https://git.example.org") == e_e
    assert problem("git", "none", "anything") == e_e
    assert problem("git", "ssh_key", "") is None
    assert problem("git", "basic", "https://git.example.org/scm") is None
    assert problem("git", "basic", "http://git.example.org") == e_a


def test_git_api_base_per_service():
    api = git_hosts.git_api_base
    assert api("github", "https://github.com") == "https://api.github.com"
    assert api("github", "https://github.com/") == "https://api.github.com"
    assert api("github", "https://octo.ghe.com") == "https://api.octo.ghe.com"
    assert api("github", "https://ghe.example.com") == "https://ghe.example.com/api/v3"
    assert api("github", "https://ghe.example.com:8443/prefix") == "https://ghe.example.com:8443/prefix/api/v3"
    assert api("gitlab", "https://gitlab.com") == "https://gitlab.com/api/v4"
    assert api("gitlab", "https://example.com/gitlab") == "https://example.com/gitlab/api/v4"
    assert api("git", "https://git.example.org") is None
    assert api("github", "") is None


def test_ships_host_keys():
    assert git_hosts.ships_host_keys("github", "https://github.com")
    assert git_hosts.ships_host_keys("gitlab", "https://gitlab.com")
    assert not git_hosts.ships_host_keys("github", "https://ghe.example.com")
    assert not git_hosts.ships_host_keys("gitlab", "https://example.com/gitlab")
    assert not git_hosts.ships_host_keys("gitlab", "https://github.com")
    assert not git_hosts.ships_host_keys("git", "https://github.com")
    assert not git_hosts.ships_host_keys("git", "")
    assert git_hosts.GIT_SERVICES == ("github", "gitlab", "git")
    assert git_hosts.KEY_TYPES.isdisjoint(git_hosts.SK_KEY_TYPES)


def test_module_is_pure():
    """No I/O, no ``gen``, no process: the module imports only these stdlib modules."""
    tree = ast.parse(Path(git_hosts.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported <= {"base64", "hashlib", "re", "struct", "dataclasses", "typing", "urllib.parse",
                        "collections.abc"}, imported
