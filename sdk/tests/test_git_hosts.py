# SPDX-License-Identifier: MIT
"""``collimate.git_hosts`` — the pure Git account checks the Key manager's Save
and Verify, the host's account carrier and the runs share: the pinned
github.com and gitlab.com host keys, known_hosts and OpenSSH private-key
parsing, the account address rules, a kind-git entry read into a
``GitAccount`` (G1–G6, which addresses its credential may serve, the secret's
spellings), the environment it gives a run, the ``GIT_CONFIG_COUNT`` builder
and the remote URL rule."""
import ast
import base64
import os
import shutil
import struct
import subprocess
import urllib.parse
from functools import partial
from pathlib import Path

import pytest

from collimate import git_hosts
from collimate.git_hosts import KnownHost
from _env_shared import git_run
from tests._git_fixtures import GIT_OK, git_account_snapshot, ssh_key


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


GH_KNOWN = "box.example.org ssh-ed25519 " + git_hosts.PINNED_HOST_KEYS["github.com"][0].split()[2]


def account(service="github", auth="api_key", **over) -> git_hosts.GitAccount:
    return git_hosts.account_from_record(git_account_snapshot(service, auth, **over))


def remote(url: str, account_=None) -> git_hosts.Remote:
    return git_run.resolve_remote(url, account_)


@pytest.mark.parametrize("service, auth, over, message", [
    ("github", "api_key", {"kind": "github"}, git_hosts.G1),
    ("github", "api_key", {"kind": "account"}, git_hosts.G1),
    ("openai", "api_key", {}, git_hosts.G1),
    ("github", "basic", {}, git_hosts.G1),
    ("git", "api_key", {}, git_hosts.G1),
    ("gitlab", "none", {}, git_hosts.G1),
    ("github", "api_key", {"metadata": {"author_name": None}}, git_hosts.G2),
    ("github", "api_key", {"metadata": {"author_email": "not-an-email"}}, git_hosts.G2),
    ("github", "api_key", {"metadata": {"author_name": "A <b>"}}, git_hosts.G2),
    ("github", "api_key", {"secrets": {}}, git_hosts.G3.format(what="token")),
    ("gitlab", "api_key", {"secrets": {"api_key": ""}}, git_hosts.G3.format(what="token")),
    ("git", "basic", {"metadata": {"username": None}}, git_hosts.G3.format(what="username and token")),
    ("git", "basic", {"secrets": {}}, git_hosts.G3.format(what="username and token")),
    ("git", "ssh_key", {"secrets": {}}, git_hosts.G3.format(what="SSH private key")),
    ("github", "api_key", {"endpoint": "https://api.github.com"}, "fix it in Key manager"),
    ("gitlab", "api_key", {"endpoint": "http://gitlab.example.com"}, "must be an https URL"),
    ("git", "basic", {"endpoint": ""}, git_hosts.G4.format(problem="no server URL")),
    ("git", "ssh_key", {"endpoint": "https://git.example.org"}, "has no server URL"),
    ("git", "ssh_key", {"secrets": {"private_key": partial(ssh_key, "ed25519_encrypted")}}, "protected by a passphrase"),
    ("git", "ssh_key", {"secrets": {"private_key": partial(ssh_key, "ed25519.pub")}}, "That is a public key"),
    ("git", "ssh_key", {"metadata": {"known_hosts": "|1|abc= ssh-ed25519 AAAA"}}, "Known hosts cannot be read"),
])
def test_account_from_record_refusals_g1_to_g6(service, auth, over, message):
    if callable(over.get("secrets", {}).get("private_key")):  # a throwaway key, made when the test runs
        over = {**over, "secrets": {"private_key": over["secrets"]["private_key"]()}}
    record = git_account_snapshot(service, auth, **over)
    assert _refusal(git_hosts.account_from_record, record).count(message) == 1
    if "address" in message or "Key manager" in message:
        assert "Key manager" in _refusal(git_hosts.account_from_record, record)
    for secret in record["secrets"].values():
        assert not secret or secret not in _refusal(git_hosts.account_from_record, record)


def test_account_from_record_refuses_what_is_not_a_record():
    for record in (None, "github", ["git"], 7):
        assert _refusal(git_hosts.account_from_record, record) == git_hosts.G1


def test_each_service_and_auth_builds_its_account():
    gh = account()
    assert (gh.service, gh.auth, gh.web, gh.username, gh.secret) == (
        "github", "api_key", "https://github.com", "x-access-token", "ghp_fake")
    assert (gh.transport, gh.forge, gh.scope(), gh.known_hosts) == ("https", True, "https://github.com/", ())
    assert gh.identity == git_hosts.Identity("Ada Lovelace", "ada@example.org")
    ghe = account(endpoint="https://GHE.example.com:443/")
    assert (ghe.web, ghe.scope()) == ("https://ghe.example.com", "https://ghe.example.com/")
    gl = account("gitlab", endpoint="https://example.com/gitlab")
    assert (gl.web, gl.username, gl.secret, gl.scope()) == (
        "https://example.com/gitlab", "oauth2", "glpat-fake", "https://example.com/gitlab/")
    basic = account("git", "basic")
    assert (basic.web, basic.username, basic.secret, basic.forge) == ("https://git.example.org", "ada", "pw-fake", False)
    none = account("git", "none")
    assert (none.web, none.username, none.secret, none.scope(), none.authorization()) == ("", "", "", "", None)
    ssh = account("git", "ssh_key", metadata={"known_hosts": GH_KNOWN})
    assert (ssh.transport, ssh.web, ssh.scope(), ssh.authorization()) == ("ssh", "", "", None)
    assert ssh.secret == git_hosts.normalise_private_key(ssh_key())
    assert ssh.known_hosts == git_hosts.pinned_known_hosts() + git_hosts.parse_known_hosts(GH_KNOWN)
    assert ssh.own_known_hosts == git_hosts.parse_known_hosts(GH_KNOWN)
    forge_ssh = account("gitlab", "ssh_key")
    assert forge_ssh.known_hosts == git_hosts.pinned_known_hosts()


def test_repr_never_carries_the_secret():
    for service, auth in (("github", "api_key"), ("git", "basic"), ("git", "ssh_key")):
        acct = account(service, auth)
        for form in acct.secret_forms():
            assert form not in repr(acct) and form not in str(acct)
        assert repr(acct).startswith(f"GitAccount({service}, {auth}, ")
    auth = git_hosts.HttpsAuth("https://h/", "c2VjcmV0")
    assert "c2VjcmV0" not in repr(auth)
    assert "PRIVATE" not in repr(git_hosts.SshAuth(ssh_key(), ("h ssh-ed25519 AAAA",)))


@pytest.mark.parametrize("service, auth, over, url, code", [
    ("github", "api_key", {}, "https://gitlab.com/g/p.git", "scope"),
    ("gitlab", "api_key", {"endpoint": "https://example.com/gitlab"}, "https://example.com/other/x.git", "scope"),
    ("gitlab", "api_key", {"endpoint": "https://example.com/gitlab"}, "https://example.com/gitlabx/x.git", "scope"),
    ("github", "api_key", {}, "git@github.com:octo/api.git", "transport"),
    ("git", "basic", {}, "https://other.example.org/x.git", "scope"),
    ("git", "basic", {}, "ssh://git@git.example.org/x.git", "transport"),
    ("git", "none", {}, "git@box.example.org:x.git", "transport"),
    ("git", "ssh_key", {}, "https://git.example.org/x.git", "transport"),
    ("git", "ssh_key", {}, "ssh://git@box.example.org:2222/srv/app.git", "no_host_key"),
    ("github", "ssh_key", {}, "ssh://git@github.com:2222/octo/api.git", "scope"),
])
def test_auth_for_refuses_scope_transport_and_unknown_host(service, auth, over, url, code):
    acct = account(service, auth, **over)
    with pytest.raises(git_hosts.RemoteRefused) as refused:
        acct.auth_for(remote(url))
    assert refused.value.code == code
    if code == "transport":
        assert set(refused.value.fields) == {"method", "scheme", "other"}
    if code == "no_host_key":
        assert refused.value.fields == {"hostport": "[box.example.org]:2222"}
    if code == "scope" and service == "gitlab":
        assert refused.value.fields == {"base": "example.com/gitlab", "host": "example.com"}


def test_auth_for_returns_the_scoped_header_or_ssh_material():
    gh = account()
    https = gh.auth_for(remote("https://github.com/octo/api.git"))
    assert https == git_hosts.HttpsAuth("https://github.com/", git_hosts.basic_authorization("x-access-token", "ghp_fake"))
    assert base64.b64decode(https.authorization).decode() == "x-access-token:ghp_fake"
    gl = account("gitlab", endpoint="https://example.com/gitlab")
    assert gl.auth_for(remote("https://example.com/gitlab/g/s/p.git")).scope == "https://example.com/gitlab/"
    assert account("git", "none").auth_for(remote("https://anywhere.example/x")) is None
    ssh = account("git", "ssh_key", metadata={"known_hosts": "[box.example.org]:2222 " + GH_KNOWN.split(" ", 1)[1]})
    material = ssh.auth_for(remote("ssh://git@box.example.org:2222/srv/app.git"))
    assert isinstance(material, git_hosts.SshAuth)
    assert material.private_key == git_hosts.normalise_private_key(ssh_key())
    assert material.known_hosts[-1].startswith("[box.example.org]:2222 ssh-ed25519 ")
    assert all(k.line() in material.known_hosts for k in git_hosts.pinned_known_hosts())
    forge = account("github", "ssh_key")
    forge_remote = remote("https://github.com/octo/api.git", forge)
    assert forge_remote == git_hosts.Remote("ssh://git@github.com/octo/api.git", ("ssh",), "github.com")
    assert isinstance(forge.auth_for(forge_remote), git_hosts.SshAuth)


def test_agent_known_hosts_trust_only_the_accounts_own_host():
    blob = GH_KNOWN.split()[2]
    gh = account("github", "ssh_key")
    assert {k.hosts for k in gh.agent_known_hosts()} == {("github.com",)}
    assert len(gh.agent_known_hosts()) == 3
    server = account("git", "ssh_key", metadata={"known_hosts": f"box.example.org ssh-ed25519 {blob}"})
    assert server.agent_known_hosts() == server.own_known_hosts


def test_secret_forms_per_auth():
    token = "ghp_a/b+c"
    gh = account(secrets={"api_key": token})
    assert gh.secret_forms() == (token, urllib.parse.quote(token, safe=""),
                                 git_hosts.basic_authorization("x-access-token", token), f"x-access-token:{token}")
    basic = account("git", "basic", metadata={"username": "a b"}, secrets={"password": "p@ss"})
    assert basic.secret_forms() == ("p@ss", "p%40ss", git_hosts.basic_authorization("a b", "p@ss"), "a%20b:p%40ss")
    forms = account("git", "ssh_key").secret_forms()
    normalised = git_hosts.normalise_private_key(ssh_key())
    body = [line for line in normalised.splitlines() if not line.startswith("-----")]
    assert forms[:4] == (normalised, normalised.rstrip("\n"), normalised.replace("\n", "\r\n"), "".join(body))
    assert set(forms[4:]) == {line for line in body if len(line) >= 16}
    assert account("git", "none").secret_forms() == ()


AUTHOR = {"GIT_AUTHOR_NAME": "Ada Lovelace", "GIT_AUTHOR_EMAIL": "ada@example.org",
          "GIT_COMMITTER_NAME": "Ada Lovelace", "GIT_COMMITTER_EMAIL": "ada@example.org"}


def _header(scope: str, user: str, secret: str) -> dict:
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{scope}.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {git_hosts.basic_authorization(user, secret)}"}


@pytest.mark.parametrize("record, expected", [
    (git_account_snapshot(), {**AUTHOR, **_header("https://github.com/", "x-access-token", "ghp_fake"),
                              "GITHUB_TOKEN": "ghp_fake", "GH_TOKEN": "ghp_fake"}),
    (git_account_snapshot(endpoint="https://ghe.example.com:8443/scm"),
     {**AUTHOR, **_header("https://ghe.example.com:8443/scm/", "x-access-token", "ghp_fake"),
      "GH_HOST": "ghe.example.com:8443", "GH_ENTERPRISE_TOKEN": "ghp_fake", "GITHUB_ENTERPRISE_TOKEN": "ghp_fake"}),
    (git_account_snapshot("gitlab", endpoint="https://example.com/gitlab"),
     {**AUTHOR, **_header("https://example.com/gitlab/", "oauth2", "glpat-fake"),
      "GITLAB_TOKEN": "glpat-fake", "GITLAB_HOST": "https://example.com/gitlab"}),
    (git_account_snapshot("git", "basic"), {**AUTHOR, **_header("https://git.example.org/", "ada", "pw-fake")}),
    (git_account_snapshot("github", "ssh_key"), AUTHOR),
    (git_account_snapshot("git", "none"), AUTHOR),
])
def test_git_account_environment_per_account(record, expected):
    """The run's environment for a bound account: the author always; a
    token's or a username and token's Basic header scoped to the account's
    base; a forge token's gh or glab names; an SSH key adds no variable."""
    env = git_hosts.git_account_environment(git_hosts.account_from_record(record))
    assert env == expected
    assert set(env) <= {"GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", *git_hosts.AUTHOR_ENV,
                        *git_hosts.FORGE_ENV}


def test_git_config_env_appends_renumbers_and_skips_a_pair_git_already_ends_with():
    header = ("http.https://github.com/.extraHeader", "Authorization: Basic eA==")
    base = {"PATH": "/usr/bin", "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.pager",
            "GIT_CONFIG_VALUE_0": "cat", "GIT_CONFIG_KEY_1": header[0], "GIT_CONFIG_VALUE_1": header[1]}
    env = git_hosts.git_config_env([("safe.directory", "*"), header], base)
    assert env == {"PATH": "/usr/bin", "GIT_CONFIG_COUNT": "3",
                   "GIT_CONFIG_KEY_0": "core.pager", "GIT_CONFIG_VALUE_0": "cat",
                   "GIT_CONFIG_KEY_1": header[0], "GIT_CONFIG_VALUE_1": header[1],
                   "GIT_CONFIG_KEY_2": "safe.directory", "GIT_CONFIG_VALUE_2": "*"}
    assert base["GIT_CONFIG_COUNT"] == "2"                     # a new dict; base is untouched
    assert git_hosts.git_config_env([("safe.directory", "*"), header], env) == env
    assert git_hosts.git_config_entries(env) == [("core.pager", "cat"), header, ("safe.directory", "*")]
    # A value-less key is an empty value; another value of a key is another pair.
    twice = git_hosts.git_config_env([("credential.helper", ""), ("credential.helper", "store")])
    assert git_hosts.git_config_entries(twice) == [("credential.helper", ""), ("credential.helper", "store")]


@pytest.mark.parametrize("held, added", [
    ([("credential.helper", "store"), ("credential.helper", "")], ("credential.helper", "store")),
    ([("credential.helper", "a"), ("credential.helper", "b")], ("credential.helper", "a")),
    ([("core.pager", "less"), ("core.pager", "cat")], ("core.pager", "less")),
])
def test_git_config_env_appends_a_value_the_key_held_earlier(held, added):
    """Order matters for a multi-valued key (helpers run in order, an empty
    value resets the list) and the last value wins for a single one: a value
    the block held before a later value is appended as any other."""
    env = git_hosts.git_config_env(held)
    assert git_hosts.git_config_entries(git_hosts.git_config_env([added], env)) == [*held, added]


def test_git_config_env_never_writes_a_count_without_its_keys():
    """git refuses GIT_CONFIG_COUNT without GIT_CONFIG_KEY_0 ("missing config
    key"): no pairs, no count; a stale block in the base goes."""
    assert git_hosts.git_config_env([]) == {}
    assert git_hosts.git_config_env([], {"GIT_CONFIG_COUNT": "0", "HOME": "/h"}) == {"HOME": "/h"}
    for broken in ({"GIT_CONFIG_COUNT": "1"}, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b"},
                   {"GIT_CONFIG_COUNT": "x"}, {"GIT_CONFIG_COUNT": "-1"},
                   {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "", "GIT_CONFIG_VALUE_0": "v"}):
        with pytest.raises(ValueError):
            git_hosts.git_config_env([("safe.directory", "*")], broken)
        with pytest.raises(ValueError):
            git_hosts.git_config_entries(broken)
    for key in ("", "nosection", "a.b\nc.d", "."):
        with pytest.raises(ValueError):
            git_hosts.git_config_env([(key, "v")])


@pytest.mark.skipif(not GIT_OK, reason="needs git 2.31 or later")
def test_the_carried_header_resolves_only_under_the_accounts_base(tmp_path):
    """Real git reads the run's block: ``get-urlmatch`` finds the header for
    a repository under the account's base and for no other path or host,
    and sees the author (P12)."""
    record = git_account_snapshot("gitlab", endpoint="https://example.com/gitlab")
    acct = git_hosts.account_from_record(record)
    env = git_hosts.hardened_env(os.environ, home=str(tmp_path), ceiling=str(tmp_path))
    env.update(git_hosts.git_account_environment(acct))
    git = shutil.which("git")

    def urlmatch(url: str) -> str:
        out = subprocess.run([git, "config", "--get-urlmatch", "http.extraHeader", url], env=env,
                             cwd=tmp_path, capture_output=True, text=True)
        return out.stdout.strip()

    assert urlmatch("https://example.com/gitlab/g/p.git") == f"Authorization: Basic {acct.authorization()}"
    for outside in ("https://example.com/other/p.git", "https://example.com/gitlabx/p.git",
                    "https://evil.example/gitlab/p.git", "http://example.com/gitlab/p.git"):
        assert urlmatch(outside) == "", outside
    ident = subprocess.run([git, "var", "GIT_AUTHOR_IDENT"], env=env, cwd=tmp_path, capture_output=True, text=True)
    assert ident.stdout.startswith("Ada Lovelace <ada@example.org> ")


@pytest.mark.parametrize("url", [
    "https://github.com/octo/api.git", "https://example.com:8443/gitlab/g/s/app.git", "https://h.io/o/r",
    "ssh://git@box.example.org:2222/srv/app.git", "ssh://box.example.org/srv/app.git",
    "git@github.com:octo/api.git", "github.com:octo/api.git", "git@box.example.org:~/r.git",
    "git@box.example.org:a::b.git", "ssh://git@[::1]:2222/srv/app.git",
])
def test_remote_problem_admits_https_ssh_and_scp(url):
    assert git_hosts.remote_problem(url) is None


@pytest.mark.parametrize("url, problem", [
    ("https://user@github.com/octo/api.git", git_hosts._R_USERINFO),
    ("https://x-access-token:ghp_s3cret@github.com/octo/api.git", git_hosts._R_USERINFO),
    ("https://github.com/octo/api.git?x=1", git_hosts._R_SHAPE),
    ("https://github.com/octo/api.git#main", git_hosts._R_SHAPE),
    ("ssh://git:pw@box.example.org/srv/app.git", git_hosts._R_PASSWORD),
    ("file:///srv/app.git", git_hosts._R_LOCAL),
    ("/srv/app.git", git_hosts._R_LOCAL),
    ("../app", git_hosts._R_LOCAL),
    ("./a:b", git_hosts._R_LOCAL),
    ("app", git_hosts._R_LOCAL),
    ("http://github.com/octo/api.git", git_hosts._R_HTTP),
    ("git://github.com/octo/api.git", git_hosts._R_GIT),
    ("ext::sh -c touch% /tmp/pwned", git_hosts._R_COMMAND),
    ("fd::17", git_hosts._R_COMMAND),
    ("persistent-https::https://h.io/r", git_hosts._R_SCHEME),
    ("ftp://h.io/r.git", git_hosts._R_SCHEME),
    ("", git_hosts._R_SHAPE),
    (" https://h.io/r", git_hosts._R_SHAPE),
    ("https://h.io/r\n[core]", git_hosts._R_SHAPE),
    ("-oProxyCommand=x:y", git_hosts._R_OPTION),
    ("git@-oProxyCommand=x:y", git_hosts._R_OPTION),
    ("-oProxyCommand=x@h:y", git_hosts._R_OPTION),
    ("git@box.example.org:-x", git_hosts._R_OPTION),
    ("ssh://-oProxyCommand=x/y", git_hosts._R_OPTION),
    ("ssh://-oProxyCommand=x@h/y", git_hosts._R_OPTION),
    ("[::1]:x", git_hosts._R_BRACKETED),
    ("user@[::1]:x", git_hosts._R_BRACKETED),
    ("[box.example.org:2222]:srv/app.git", git_hosts._R_BRACKETED),
])
def test_remote_problem_refuses_userinfo_local_cleartext_and_helpers(url, problem):
    assert git_hosts.remote_problem(url) == problem
    if "ghp_s3cret" in url:
        assert "ghp_s3cret" not in problem


def test_remote_problem_test_hook_admits_only_its_schemes(monkeypatch):
    """fake_remote's ``file://`` repositories are admitted only through the
    hook (R14); local paths and other schemes stay refused."""
    monkeypatch.setattr(git_hosts, "_ADMITTED", ("file",))
    assert git_hosts.remote_problem("file:///srv/fake/octo/api.git") is None
    assert git_hosts.remote_problem("/srv/fake/octo/api.git") == git_hosts._R_LOCAL
    assert git_hosts.remote_problem("http://h.io/r") == git_hosts._R_HTTP


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
