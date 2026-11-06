# SPDX-License-Identifier: MIT
"""Pure facts and checks for Git accounts and for running git: the published host keys of github.com
and gitlab.com, known_hosts and OpenSSH private-key parsing, the account address rules, a Key manager
Git entry read into a :class:`GitAccount` (G1-G6) and the environment it gives a run (the scoped
Basic header, the forge names, the commit author), the one builder of git's ``GIT_CONFIG_COUNT``
block, the remote URL rule, and the hardening every git child gets (the environment scrub, the
``-c`` flags, the version floor). No I/O, no process: the Key manager's Save and Verify, the host's
account carrier, the runs and the host's own git share it."""
import base64
import hashlib
import re
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import quote, urlsplit

__all__ = [
    "AUTHOR_ENV",
    "DROPPED_ENV",
    "FORGE_ENV",
    "G1",
    "G2",
    "G3",
    "G4",
    "G5",
    "G6",
    "GIT_SERVICES",
    "HARDENING_FLAGS",
    "KEY_TYPES",
    "MIN_GIT",
    "PINNED_FINGERPRINTS",
    "PINNED_HOST_KEYS",
    "SK_KEY_TYPES",
    "GitAccount",
    "HttpsAuth",
    "Identity",
    "KeyInfo",
    "KnownHost",
    "Remote",
    "RemoteRefused",
    "SshAuth",
    "account_from_record",
    "basic_authorization",
    "endpoint_problem",
    "fingerprint",
    "git_account_environment",
    "git_api_base",
    "git_base",
    "git_config_entries",
    "git_config_env",
    "hardened_env",
    "host_key_name",
    "inspect_private_key",
    "normalise_private_key",
    "parse_known_hosts",
    "pinned_known_hosts",
    "protocol_flags",
    "remote_problem",
    "ships_host_keys",
]

# The oldest git the host and the runs accept (``git --version``).
MIN_GIT = (2, 31)

# The forge tools' names a token account gives a run (gh's, then glab's).
FORGE_ENV: frozenset[str] = frozenset({
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_HOST",
    "GITLAB_TOKEN", "GITLAB_HOST",
})

# The commit author a bound account gives a run (D8).
AUTHOR_ENV: tuple[str, ...] = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")

# Ambient variables a git child must never see, beside every ``GIT_*``
# variable (traces, config injection, askpass, ssh command): the tool tokens,
# askpass helpers and SSH agent of the run.
DROPPED_ENV: frozenset[str] = FORGE_ENV | {"SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "SSH_AUTH_SOCK"}

# The ``-c`` pairs before every git subcommand, whatever the transport: no
# credential helper or prompt, no hooks, no fsmonitor, no automatic gc or
# maintenance, no submodule recursion, no signing.
HARDENING_FLAGS: tuple[str, ...] = (
    "-c", "credential.helper=", "-c", "core.askPass=", "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
    "-c", "submodule.recurse=false", "-c", "fetch.recurseSubmodules=false", "-c", "commit.gpgSign=false",
)


def protocol_flags(protocols: Sequence[str]) -> list[str]:
    """The ``-c`` pairs pinning git to exactly ``protocols`` (``protocol.allow=never`` first)."""
    return ["-c", "protocol.allow=never", *[x for p in protocols for x in ("-c", f"protocol.{p}.allow=always")]]


def hardened_env(base: Mapping[str, str], *, home: str, ceiling: str) -> dict[str, str]:
    """A new environment for a git child: ``base`` without its ``GIT_*`` and
    :data:`DROPPED_ENV` variables, HOME and XDG_CONFIG_HOME at ``home``, no
    system or global config, no terminal prompt, C locale. ``ceiling`` is
    ``GIT_CEILING_DIRECTORIES``: repository discovery never climbs above it,
    so a repository another user planted higher up supplies nothing."""
    env = {key: value for key, value in base.items() if not key.startswith("GIT_") and key not in DROPPED_ENV}
    env.update(HOME=home, XDG_CONFIG_HOME=home, GIT_CEILING_DIRECTORIES=ceiling, GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0", LC_ALL="C", LANGUAGE="")
    return env


_CONFIG_NAME = re.compile(r"GIT_CONFIG_(?:COUNT|KEY_[0-9]+|VALUE_[0-9]+)")


def git_config_entries(env: Mapping[str, str]) -> "list[tuple[str, str]]":
    """The ``(key, value)`` pairs of ``env``'s ``GIT_CONFIG_COUNT`` block, in order; empty when it has
    none. ValueError for a block git itself refuses: a count that is not a number, or a missing key or
    value."""
    count = env.get("GIT_CONFIG_COUNT")
    if count is None:
        return []
    if not count.isdigit():
        raise ValueError("GIT_CONFIG_COUNT is not a number")
    pairs = []
    for n in range(int(count)):
        key, value = env.get(f"GIT_CONFIG_KEY_{n}"), env.get(f"GIT_CONFIG_VALUE_{n}")
        if not key or value is None:
            raise ValueError(f"missing config key or value GIT_CONFIG_KEY_{n}")
        pairs.append((key, value))
    return pairs


def git_config_env(entries: "Iterable[tuple[str, str]]", base: "Mapping[str, str] | None" = None) -> dict[str, str]:
    """A new environment: ``base`` (or nothing) with ``entries`` appended to its ``GIT_CONFIG_COUNT``
    block, renumbered from 0. A pair whose key's last value in the block is already its value is not
    appended again (git would read the same, and a repeated extraHeader would be sent twice), so a leg
    forwarded twice is forwarded once. The only writer of ``GIT_CONFIG_COUNT``,
    ``GIT_CONFIG_KEY_<n>`` and ``GIT_CONFIG_VALUE_<n>``: with no pairs there is no count (git refuses a
    count without its keys, "missing config key GIT_CONFIG_KEY_0"). ValueError for a malformed block in
    ``base`` or a key without a section."""
    env = dict(base or {})
    pairs = git_config_entries(env)
    for name in [name for name in env if _CONFIG_NAME.fullmatch(name)]:
        del env[name]
    for key, value in entries:
        if "." not in key.strip(".") or "\n" in key:
            raise ValueError(f"not a git config key: {key!r}")
        values = [held for name, held in pairs if name == key]
        if not values or values[-1] != value:
            pairs.append((key, value))
    if pairs:
        env["GIT_CONFIG_COUNT"] = str(len(pairs))
        for n, (key, value) in enumerate(pairs):
            env[f"GIT_CONFIG_KEY_{n}"] = key
            env[f"GIT_CONFIG_VALUE_{n}"] = value
    return env


# The account services of kind git, in catalog order.
GIT_SERVICES: tuple[str, ...] = ("github", "gitlab", "git")

# The published SSH host keys (ssh-keyscan -t ed25519,ecdsa,rsa), kept only where their SHA256 equals
# PINNED_FINGERPRINTS, the fingerprints GitHub and GitLab publish. Runs trust these and never a host on first use.
PINNED_HOST_KEYS: Mapping[str, tuple[str, ...]] = {
    "github.com": (
        "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl",
        "github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezO"
        "mxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=",
        "github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNU"
        "kY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV"
        "9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7"
        "GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S"
        "4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmP"
        "UWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=",
    ),
    "gitlab.com": (
        "gitlab.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAfuCHKVTjquxvt6CM6tdG4SLp1Btn/nOeHHE5UOzRdf",
        "gitlab.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBFSMqzJeV9rUz"
        "U4kWitGjeR4PWSa29SPqJ1fVkhtj3Hw9xjLVXVYrU9QlYWrOLXBpQ6KWjbjTDTdDkoohFzgbEY=",
        "gitlab.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCsj2bNKTBSpIYDEGk9KxsGh3mySTRgMtXL583qmBpzeQ+jq"
        "CMRgBqB98u3z++J1sKlXHWfM9dyhSevkMwSbhoR8XIq/U0tCNyokEi/ueaBMCvbcTHhO7FcwzY92WK4Yt0aGROY5qX2UKSeO"
        "vuP4D6TPqKF1onrSzH9bx9XUf2lEdWT/ia1NEKjunUqu1xOB/StKDHMoX4/OKyIzuS0q/T1zOATthvasJFoPrAjkohTyaDUz"
        "2LN5JoH839hViyEG82yB+MjcFV5MU3N1l1QL3cVUCh93xSaua1N85qivl+siMkPGbO5xR/En4iEY6K2XPASUEMaieWVNTRCt"
        "J4S8H+9",
    ),
}

PINNED_FINGERPRINTS: Mapping[str, frozenset[str]] = {
    "github.com": frozenset({
        "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU",
        "SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM",
        "SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s",
    }),
    "gitlab.com": frozenset({
        "SHA256:eUXGGm1YGsMAS7vkcx6JOJdOGHPem5gQp4taiCfCLB8",
        "SHA256:HbW3g8zUjNSksFbqTiUWPWg2Bq1x8xdGUrliXFzSnUw",
        "SHA256:ROQFvPThGrW4RuWLoL9tq9I9zJ42fK4XywyRtbOz/EQ",
    }),
}

KEY_TYPES: frozenset[str] = frozenset({
    "ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa",
})
# Security-key (FIDO) types, recognised only to refuse them with their own message: a run cannot touch a hardware key.
SK_KEY_TYPES: frozenset[str] = frozenset({"sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com"})

_KEYINFO_TYPES = {"ssh-ed25519": "ED25519", "ecdsa-sha2-nistp256": "ECDSA", "ecdsa-sha2-nistp384": "ECDSA",
                  "ecdsa-sha2-nistp521": "ECDSA", "ssh-rsa": "RSA"}

# The host rule of a repository's canonical address (lowercase labels, a single label allowed, no IPv6, never a
# leading "-").
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")
_BRACKETED = re.compile(r"\[([^\]]*)\]:([0-9]{1,5})")
_SEGMENT = re.compile(r"[A-Za-z0-9_.+%][A-Za-z0-9_.+%~-]{0,99}")
_KEYTYPE_SHAPE = re.compile(r"[a-z0-9][a-z0-9@.-]{0,63}")
_KNOWN_HOSTS_LINES = 64
_KNOWN_HOSTS_BYTES = 16 * 1024
_PRIVATE_KEY_BYTES = 16 * 1024

_BEGIN = "-----BEGIN OPENSSH PRIVATE KEY-----"
_END = "-----END OPENSSH PRIVATE KEY-----"
_MAGIC = b"openssh-key-v1\0"
_OLD_FORMAT = re.compile(r"-----BEGIN (?:RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY-----")
_BASE64_LINE = re.compile(r"[A-Za-z0-9+/=]+")

_PUBLIC_KEY = "That is a public key; paste the private key (the file without .pub)"
_PUTTY = "This is a PuTTY key; export it from PuTTYgen with Conversions › Export OpenSSH key"
_OLD = ("This is an old-format key; rewrite a copy in OpenSSH format with ssh-keygen -p -N \"\" -f <copy> "
        "and paste that")
_UNREADABLE = ("The SSH key could not be read; paste the whole private key file, from "
               "-----BEGIN OPENSSH PRIVATE KEY----- to -----END OPENSSH PRIVATE KEY-----")
_PASSPHRASE = ("This SSH key is protected by a passphrase, which Collimate cannot use. Paste an unencrypted copy "
               "(ssh-keygen -p -N \"\" -f <copy>) or a dedicated deploy key without a passphrase")
_SECURITY_KEY = ("This is a security-key (FIDO) SSH key, which needs its hardware key touched at every use; runs "
                 "cannot do that. Create an Ed25519 key without a passphrase (ssh-keygen -t ed25519 -N \"\") "
                 "for Collimate")
_UNKNOWN_TYPE = ("DSA and unknown key types are refused by GitHub and GitLab; create an Ed25519 key "
                 "(ssh-keygen -t ed25519)")

_E_A = "A Git account's address must be an https URL"
_E_B = "A Git account's address has no user name, password, query or fragment"
_E_C = ("A GitHub account's address is where its repositories open in a browser (https://github.com, or your "
        "GitHub Enterprise server), not its API URL")
_E_D = ("A GitLab account's address is the instance's address (https://gitlab.com, or https://host[/path]), "
        "not its /api/v4")
_E_E = "A Git server account with an SSH key or no credentials has no server URL"

_FORGE_DEFAULTS = {"github": "https://github.com", "gitlab": "https://gitlab.com"}


@dataclass(frozen=True)
class KnownHost:
    """One known_hosts entry: its host names ("host" or "[host]:port", lowercase), key type and base64 blob."""

    hosts: tuple[str, ...]
    keytype: str
    blob: str

    def line(self) -> str:
        return f"{','.join(self.hosts)} {self.keytype} {self.blob}"


@dataclass(frozen=True)
class KeyInfo:
    """What an accepted private key is: "ED25519" | "ECDSA" | "RSA", the SHA256 fingerprint of its public
    blob, and its authorized_keys line (no comment)."""

    keytype: str
    fingerprint: str
    public_key: str


def host_key_name(host: str, port: "int | None") -> str:
    """The name ssh looks a host up by in known_hosts: "host" on port 22, else "[host]:port"."""
    host = host.lower()
    return host if port in (None, 22) else f"[{host}]:{port}"


def fingerprint(blob_b64: str) -> str:
    """OpenSSH's SHA256 fingerprint of a base64 public-key blob."""
    digest = hashlib.sha256(base64.b64decode(blob_b64, validate=True)).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _ssh_string(data: bytes, offset: int) -> "tuple[bytes, int]":
    """The SSH wire string at ``offset`` and the offset after it; ValueError when truncated."""
    if offset + 4 > len(data):
        raise ValueError("truncated")
    (length,) = struct.unpack_from(">I", data, offset)
    end = offset + 4 + length
    if end > len(data):
        raise ValueError("truncated")
    return data[offset + 4:end], end


def _blob_type(blob: bytes) -> str:
    """The key type a public-key blob names first; ValueError when it has none."""
    name, _ = _ssh_string(blob, 0)
    return name.decode("ascii")


def _known_host_name(entry: str) -> "str | None":
    """A known_hosts host entry, lowercased and named the way host_key_name names it; None when it breaks the
    host rule."""
    entry = entry.lower()
    port = None
    bracketed = _BRACKETED.fullmatch(entry)
    if bracketed:
        entry, port = bracketed.group(1), int(bracketed.group(2))
        if not 1 <= port <= 65535:
            return None
    if len(entry) > 253 or not _HOST.fullmatch(entry):
        return None
    return host_key_name(entry, port)


def _known_host(text: str, n: int) -> KnownHost:
    fields = text.split()
    if fields[0] in ("@cert-authority", "@revoked"):
        raise ValueError(f"Known hosts line {n} uses {fields[0]}, which runs do not support")
    not_a_line = (f"Known hosts line {n} is not a known_hosts line (host keytype base64); "
                  "paste the output of ssh-keyscan")
    if len(fields) < 3 or fields[0].startswith("@"):
        raise ValueError(not_a_line)
    names, keytype, blob = fields[0], fields[1], fields[2]
    if names.startswith("|"):
        raise ValueError(f"Known hosts line {n} is hashed; paste the unhashed line (ssh-keyscan without -H)")
    if any(c in names for c in "*?!"):
        raise ValueError(f"Known hosts line {n} uses a wildcard; pin each host by name")
    hosts = tuple(_known_host_name(entry) for entry in names.split(","))
    if any(host is None for host in hosts):
        raise ValueError(not_a_line)
    if keytype not in KEY_TYPES:
        if _KEYTYPE_SHAPE.fullmatch(keytype):
            raise ValueError(f"Known hosts line {n} has an unsupported key type {keytype}")
        raise ValueError(f"Known hosts line {n} has an unsupported key type")
    try:
        decoded = _blob_type(base64.b64decode(blob, validate=True))
    except (ValueError, UnicodeDecodeError):
        decoded = None
    if decoded != keytype:
        raise ValueError(f"Known hosts line {n} has a key that does not decode as {keytype}")
    return KnownHost(tuple(dict.fromkeys(hosts)), keytype, blob)


def parse_known_hosts(text: str) -> "tuple[KnownHost, ...]":
    """Every known_hosts entry of ``text`` (blank lines and ``#`` comments skipped), or ValueError naming the
    first line that is not a plain, pinned host key. A refusal never echoes the pasted text beyond a word
    shaped like a key type name, so a token pasted by mistake never reaches a message."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    taken = [(n, line.strip()) for n, line in enumerate(lines, 1)
             if line.strip() and not line.strip().startswith("#")]
    if len(text.encode("utf-8")) > _KNOWN_HOSTS_BYTES or len(taken) > _KNOWN_HOSTS_LINES:
        raise ValueError("Known hosts holds more than 64 lines or 16 KiB")
    return tuple(_known_host(line, n) for n, line in taken)


def pinned_known_hosts() -> "tuple[KnownHost, ...]":
    """The shipped host keys of github.com and gitlab.com."""
    return tuple(parse_known_hosts("\n".join(line for lines in PINNED_HOST_KEYS.values() for line in lines)))


def normalise_private_key(text: str) -> str:
    """The pasted key stripped, with LF line ends and exactly one trailing LF."""
    return text.strip().replace("\r\n", "\n").replace("\r", "\n") + "\n"


def _key_bytes(text: str) -> bytes:
    """The decoded body of exactly one OpenSSH armoured block; ValueError otherwise."""
    if len(text.encode("utf-8")) > _PRIVATE_KEY_BYTES:
        raise ValueError(_UNREADABLE)
    lines = text.rstrip("\n").split("\n")
    body = lines[1:-1]
    if (len(lines) < 3 or lines[0] != _BEGIN or lines[-1] != _END
            or not all(_BASE64_LINE.fullmatch(line) for line in body)):
        raise ValueError(_UNREADABLE)
    try:
        return base64.b64decode("".join(body), validate=True)
    except ValueError:
        raise ValueError(_UNREADABLE) from None


def inspect_private_key(text: str) -> KeyInfo:
    """What an unencrypted OpenSSH private key is, or ValueError with the fix for every key a run cannot use
    (a public key, PuTTY or old PEM formats, a passphrase, a security key, DSA or an unknown type)."""
    text = normalise_private_key(text)
    if any(text.startswith(f"{name} AAAA") for name in KEY_TYPES | SK_KEY_TYPES):
        raise ValueError(_PUBLIC_KEY)
    if text.startswith("PuTTY-User-Key-File"):
        raise ValueError(_PUTTY)
    if _OLD_FORMAT.search(text):
        raise ValueError(_OLD)
    data = _key_bytes(text)
    try:
        if not data.startswith(_MAGIC):
            raise ValueError("magic")
        offset = len(_MAGIC)
        cipher, offset = _ssh_string(data, offset)
        kdf, offset = _ssh_string(data, offset)
        _, offset = _ssh_string(data, offset)
        if offset + 4 > len(data):
            raise ValueError("truncated")
        (nkeys,) = struct.unpack_from(">I", data, offset)
        if nkeys != 1:
            raise ValueError("nkeys")
        public, offset = _ssh_string(data, offset + 4)
        private, offset = _ssh_string(data, offset)
        keytype = _blob_type(public)
    except (ValueError, UnicodeDecodeError):
        raise ValueError(_UNREADABLE) from None
    if cipher != b"none" or kdf != b"none":
        raise ValueError(_PASSPHRASE)
    if keytype in SK_KEY_TYPES:
        raise ValueError(_SECURITY_KEY)
    if keytype not in KEY_TYPES:
        raise ValueError(_UNKNOWN_TYPE)
    try:
        if len(private) < 8:
            raise ValueError("truncated")
        check1, check2 = struct.unpack_from(">II", private, 0)
        private_type, _ = _ssh_string(private, 8)
    except ValueError:
        raise ValueError(_UNREADABLE) from None
    if check1 != check2 or private_type != keytype.encode("ascii"):
        raise ValueError(_UNREADABLE)
    blob = base64.b64encode(public).decode("ascii")
    return KeyInfo(_KEYINFO_TYPES[keytype], fingerprint(blob), f"{keytype} {blob}")


def ships_host_keys(service: str, base: str) -> bool:
    """Whether runs already trust the account's SSH host: github.com for a GitHub entry on https://github.com,
    gitlab.com for a GitLab entry on https://gitlab.com."""
    return service in _FORGE_DEFAULTS and base.rstrip("/") == _FORGE_DEFAULTS[service]


def git_base(endpoint: str) -> str:
    """A Git account's address normalised (lowercase scheme and host, no port 443, no trailing "/"), or
    ValueError (E-A, E-B)."""
    endpoint = endpoint.strip()
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        raise ValueError(_E_A) from None
    if parts.scheme.lower() != "https" or not parts.netloc:
        raise ValueError(_E_A)
    if "@" in parts.netloc or "?" in endpoint or "#" in endpoint:
        raise ValueError(_E_B)
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    if (len(host) > 253 or not _HOST.fullmatch(host) or port == 0
            or (path and not all(_SEGMENT.fullmatch(s) and s not in (".", "..") for s in path[1:].split("/")))):
        raise ValueError(_E_A)
    netloc = host if port in (None, 443) else f"{host}:{port}"
    return f"https://{netloc}{path}"


def endpoint_problem(service: str, auth: str, endpoint: str) -> "str | None":
    """Why a kind-git entry's endpoint cannot be used (E-A..E-E), or None. An empty endpoint has no problem here:
    the caller supplies the catalog default or asks for the server URL."""
    if not endpoint.strip():
        return None
    if service == "git" and auth in ("ssh_key", "none"):
        return _E_E
    try:
        base = git_base(endpoint)
    except ValueError as exc:
        return str(exc)
    parts = urlsplit(base)
    host = parts.hostname or ""
    if service == "github" and (host == "api.github.com" or (host.startswith("api.") and host.endswith(".ghe.com"))
                                or parts.path.endswith("/api/v3")):
        return _E_C
    if service == "gitlab" and parts.path.endswith("/api/v4"):
        return _E_D
    return None


def git_api_base(service: str, base: str) -> "str | None":
    """The REST API base Verify signs in to: GitHub's api.github.com, api.<sub>.ghe.com or <base>/api/v3;
    GitLab's <base>/api/v4; None for a Git server entry or an empty base."""
    base = base.rstrip("/")
    if not base:
        return None
    if service == "github":
        if base == "https://github.com":
            return "https://api.github.com"
        parts = urlsplit(base)
        host = parts.hostname or ""
        sub = host.removesuffix(".ghe.com")
        if host.endswith(".ghe.com") and sub and "." not in sub and parts.port is None and not parts.path:
            return f"https://api.{sub}.ghe.com"
        return f"{base}/api/v3"
    if service == "gitlab":
        return f"{base}/api/v4"
    return None


# ── the run's Git account ──────────────────────────────────────────────

Service = Literal["github", "gitlab", "git"]
Auth = Literal["api_key", "ssh_key", "basic", "none"]

_AUTHS: dict[str, tuple[str, ...]] = {"github": ("api_key", "ssh_key"), "gitlab": ("api_key", "ssh_key"),
                                      "git": ("ssh_key", "basic", "none")}
_FORGE_WEB = {"github": "https://github.com", "gitlab": "https://gitlab.com"}
_TOKEN_USER = {"github": "x-access-token", "gitlab": "oauth2"}
_EMAIL = re.compile(r"[^@\s<>]+@[^@\s<>]+")

G1 = "The git account role needs a GitHub, GitLab or Git server entry from Key manager"
G2 = "The selected Git account has no valid commit author name and email; set them in Key manager"
G3 = "The selected Git account has no {what}; add it in Key manager"
G4 = "The selected Git account's address cannot be used ({problem}); fix it in Key manager"
G5 = "The selected Git account's SSH key cannot be used ({problem}); replace it in Key manager"
G6 = "The selected Git account's Known hosts cannot be read ({problem}); fix them in Key manager"


@dataclass(frozen=True)
class Remote:
    """The address git is handed and the one transport it may use for it:
    ``https://host[:port]/path``, ``ssh://user@host[:port]/path`` or scp
    ``user@host:path`` (protocol ``ssh``); ``file://`` only in tests.
    ``known_host`` is the name ssh looks the host up by (``host`` or
    ``[host]:port``), ``""`` for https."""
    url: str
    protocols: "tuple[str, ...]"                 # exactly one: the ONLY transport git may use for it
    known_host: str = ""


@dataclass(frozen=True)
class HttpsAuth:
    scope: str                                   # the account's base, ending with "/"
    authorization: str = field(repr=False)       # base64(user:secret)


@dataclass(frozen=True)
class SshAuth:
    private_key: str = field(repr=False)         # normalised OpenSSH text, one trailing LF
    known_hosts: "tuple[str, ...]" = ()          # known_hosts lines


@dataclass(frozen=True)
class Identity:
    name: str
    email: str


def basic_authorization(username: str, secret: str) -> str:
    """The HTTP Basic credential ``base64(username:secret)``."""
    return base64.b64encode(f"{username}:{secret}".encode("utf-8")).decode("ascii")


class RemoteRefused(ValueError):
    """The account cannot serve this address. ``fields`` hold only validated
    values (base, host, hostport, method, scheme, other) for the message."""

    def __init__(self, code: 'Literal["scope", "transport", "no_host_key"]', **fields: str) -> None:
        super().__init__(f"the Git account cannot serve this remote ({code})")
        self.code = code
        self.fields = dict(fields)


@dataclass(frozen=True)
class _Address:
    scheme: str                 # "https" | "ssh" (scp included)
    host: str
    port: "int | None"
    path: str


def _address(url: str) -> _Address:
    if url.startswith("https://") or url.startswith("ssh://"):
        parts = urlsplit(url)
        return _Address(parts.scheme, (parts.hostname or "").lower(), parts.port, parts.path)
    _user, _, rest = url.partition("@")
    host, _, path = rest.partition(":")
    return _Address("ssh", host.lower(), None, path)


def _hostport(host: str, port: "int | None") -> str:
    return host if port is None else f"{host}:{port}"


@dataclass(frozen=True, repr=False)
class GitAccount:
    service: Service
    auth: Auth
    web: str                                   # forge web base | git+basic Server URL | "" (no trailing "/")
    username: str                              # Basic user: x-access-token | oauth2 | metadata.username | ""
    secret: str = field(repr=False)            # token, password or normalised private key; "" for none
    known_hosts: "tuple[KnownHost, ...]" = ()  # ssh_key: the pinned keys plus the account's own lines
    identity: Identity = field(default_factory=lambda: Identity("", ""))
    own_known_hosts: "tuple[KnownHost, ...]" = ()  # ssh_key: the lines pasted in the account's Known hosts

    @property
    def transport(self) -> 'Literal["https", "ssh"]':
        return "ssh" if self.auth == "ssh_key" else "https"

    @property
    def forge(self) -> bool:
        return self.service in ("github", "gitlab")

    def scope(self) -> str:
        """The base the HTTPS credential is sent under ("" when there is none)."""
        return self.web + "/" if self.auth in ("api_key", "basic") else ""

    def authorization(self) -> "str | None":
        if self.auth in ("api_key", "basic"):
            return basic_authorization(self.username, self.secret)
        return None

    def secret_forms(self) -> "tuple[str, ...]":
        """Every spelling of the secret a child, a log or an error could carry."""
        if self.auth == "api_key":
            token = self.secret
            return (token, quote(token, safe=""), basic_authorization(self.username, token),
                    f"{self.username}:{token}")
        if self.auth == "basic":
            password = self.secret
            return (password, quote(password, safe=""), basic_authorization(self.username, password),
                    f"{quote(self.username, safe='')}:{quote(password, safe='')}")
        if self.auth == "ssh_key":
            key = self.secret
            body = [line for line in key.splitlines() if line and not line.startswith("-----")]
            forms = [key, key.removesuffix("\n"), key.replace("\n", "\r\n"), "".join(body)]
            forms += [line for line in body if len(line) >= 16]
            return tuple(dict.fromkeys(f for f in forms if f))
        return ()

    def agent_known_hosts(self) -> "tuple[KnownHost, ...]":
        """The host keys an agent's ssh may trust, so its key goes only where
        :meth:`auth_for` would send it: on a forge, the keys (shipped or the
        account's own) of its web host on port 22, under that name alone; on
        a Git server, the account's own lines."""
        if not self.forge:
            return self.own_known_hosts
        host = self._web_address().host
        return tuple(dict.fromkeys(KnownHost((host,), known.keytype, known.blob)
                                   for known in self.known_hosts if host in known.hosts))

    def _web_address(self) -> _Address:
        return _address(self.web)

    def _base_label(self) -> str:
        web = self._web_address()
        return _hostport(web.host, web.port) + web.path

    def _under_web(self, remote: _Address) -> bool:
        web = self._web_address()
        prefix = web.path.rstrip("/") + "/"
        return (remote.scheme == "https" and remote.host == web.host and remote.port == web.port
                and remote.path.startswith(prefix))

    def auth_for(self, remote: Remote) -> "HttpsAuth | SshAuth | None":
        """The credential for ``remote`` (production address); raises
        :class:`RemoteRefused` before any process starts when the account
        cannot serve it."""
        address = _address(remote.url)
        refused_scope = RemoteRefused("scope", base=self._base_label() if self.web else "",
                                      host=_hostport(address.host, address.port))
        if self.forge:
            web = self._web_address()
            if address.host != web.host:
                raise refused_scope
            if address.scheme == "https" and not self._under_web(address):
                raise refused_scope
            if address.scheme == "ssh" and address.port not in (None, 22):
                raise refused_scope
        elif self.auth == "basic" and address.scheme == "https" and not self._under_web(address):
            raise refused_scope
        if self.auth == "ssh_key":
            if remote.protocols != ("ssh",):
                raise RemoteRefused("transport", method="an SSH key", scheme="https", other="SSH")
            if not any(remote.known_host in known.hosts for known in self.known_hosts):
                raise RemoteRefused("no_host_key", hostport=remote.known_host)
            return SshAuth(self.secret, tuple(known.line() for known in self.known_hosts))
        if self.auth in ("api_key", "basic"):
            if remote.protocols != ("https",):
                raise RemoteRefused("transport", method="a token" if self.auth == "api_key"
                                    else "a username and token", scheme="SSH", other="https")
            return HttpsAuth(self.scope(), self.authorization() or "")
        if remote.protocols != ("https",):
            raise RemoteRefused("transport", method="no credentials", scheme="SSH", other="https")
        return None

    def __repr__(self) -> str:
        return f"GitAccount({self.service}, {self.auth}, {self.web or '-'})"


def _author(meta: dict) -> Identity:
    name = meta.get("author_name")
    email = meta.get("author_email")
    if not isinstance(name, str) or not isinstance(email, str):
        raise ValueError(G2)
    name, email = name.strip(), email.strip()
    if (not name or len(name) > 200 or any(c in name for c in "<>\0\n\r")
            or len(email) > 254 or not _EMAIL.fullmatch(email)):
        raise ValueError(G2)
    return Identity(name, email)


def account_from_record(account: dict) -> GitAccount:
    """A Key manager kind-git entry's private snapshot (``gen.account("git")``,
    the host's run account context) read into a :class:`GitAccount`. Raises
    ``ValueError`` with the fix (G1–G6) when the entry cannot serve. Never
    puts a snapshot field in a message."""
    if not isinstance(account, dict):
        raise ValueError(G1)
    service, auth = account.get("service"), account.get("auth_type")
    if (account.get("kind") != "git" or service not in GIT_SERVICES
            or auth not in _AUTHS.get(service, ())):
        raise ValueError(G1)
    meta = account.get("metadata") if isinstance(account.get("metadata"), dict) else {}
    identity = _author(meta)
    secrets = account.get("secrets") if isinstance(account.get("secrets"), dict) else {}
    endpoint = str(account.get("endpoint") or "")
    problem = endpoint_problem(service, auth, endpoint)
    if problem is not None:
        raise ValueError(G4.format(problem=problem))
    if service in _FORGE_WEB:
        web = git_base(endpoint or _FORGE_WEB[service])
    elif auth == "basic":
        if not endpoint.strip():
            raise ValueError(G4.format(problem="no server URL"))
        web = git_base(endpoint)
    else:
        web = ""
    username, secret, known, own = "", "", (), ()
    if auth == "api_key":
        secret = secrets.get("api_key")
        if not isinstance(secret, str) or not secret:
            raise ValueError(G3.format(what="token"))
        username = _TOKEN_USER[service]
    elif auth == "basic":
        secret, username = secrets.get("password"), meta.get("username")
        if not isinstance(secret, str) or not secret or not isinstance(username, str) or not username:
            raise ValueError(G3.format(what="username and token"))
    elif auth == "ssh_key":
        key = secrets.get("private_key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError(G3.format(what="SSH private key"))
        secret = normalise_private_key(key)
        try:
            inspect_private_key(secret)
        except ValueError as exc:
            raise ValueError(G5.format(problem=exc)) from None
        try:
            own = parse_known_hosts(str(meta.get("known_hosts") or ""))
        except ValueError as exc:
            raise ValueError(G6.format(problem=exc)) from None
        known = pinned_known_hosts() + own
    return GitAccount(service, auth, web, username, secret, known, identity, own)


def git_account_environment(account: GitAccount) -> dict[str, str]:
    """What a bound Git account puts in a run's environment (the host's
    ``secret_environment`` kind ``git``): the commit author and committer;
    for a token or a username and token, git's Basic header as
    ``http.<base>/.extraHeader``, so git sends it only under the account's
    base (T1); for a forge token also the forge tool's names, gh's
    (``GH_HOST`` and the enterprise names off github.com) or glab's. An SSH
    key adds nothing: it stays a file the run writes for itself."""
    name, email = account.identity.name, account.identity.email
    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
           "GIT_COMMITTER_EMAIL": email}
    if account.auth in ("api_key", "basic"):
        env = git_config_env([(f"http.{account.scope()}.extraHeader",
                               f"Authorization: Basic {account.authorization()}")], env)
    if account.auth != "api_key":
        return env
    if account.service == "gitlab":
        env.update(GITLAB_TOKEN=account.secret, GITLAB_HOST=account.web)
    elif account.web == _FORGE_WEB["github"]:
        env.update(GITHUB_TOKEN=account.secret, GH_TOKEN=account.secret)
    else:
        env.update(GH_HOST=urlsplit(account.web).netloc, GH_ENTERPRISE_TOKEN=account.secret,
                   GITHUB_ENTERPRISE_TOKEN=account.secret)
    return env


# ── remote URLs ────────────────────────────────────────────────────────

# Schemes remote_problem admits beyond https and ssh: the test hook, which
# fake_remote widens to its local ``file://`` repositories (R14).
_ADMITTED: "tuple[str, ...]" = ()

_R_USERINFO = "a remote address carries no user name or password over https (the Git account supplies it)"
_R_PASSWORD = "an ssh remote address carries no password"
_R_LOCAL = "a local path or file:// is not a remote here"
_R_HTTP = "plain http:// sends everything in the clear; use https://"
_R_GIT = "git:// is unauthenticated and unencrypted; use https:// or ssh://"
_R_COMMAND = "ext:: and fd:: remotes run commands and are not allowed"
_R_SCHEME = "only https://, ssh:// and user@host:path remotes are allowed"
_R_SHAPE = "the remote address cannot be read"
_R_OPTION = "a remote address's user, host or path cannot start with '-'"
_R_BRACKETED = "write a bracketed host as ssh://[host]/path"
_SCP_REMOTE = re.compile(r"((?:[^@/:\s]+@)?([^@/:\s\[\]]+)):(?!//)(\S.*)", re.DOTALL)
# git's transport helper syntax, ``<transport>::<address>``.
_HELPER_REMOTE = re.compile(r"[A-Za-z0-9+.-]+::")


def remote_problem(url: str) -> "str | None":
    """Why ``url`` (a ``git remote get-url`` answer) cannot be a run's
    remote, or None: https without user info, query or fragment; ``ssh://``
    without a password; scp ``[user@]host:path``. Local paths, ``file://``,
    ``http://``, ``git://``, the ``ext::``/``fd::`` and other transport
    helpers, and a user, host or scp path shaped like an ssh option are
    refused."""
    if not url or url != url.strip() or any(c in url for c in "\0\n\r"):
        return _R_SHAPE
    lowered = url.lower()
    if lowered.startswith(("ext::", "fd::")):
        return _R_COMMAND
    scheme, sep, _rest = lowered.partition("://")
    if sep and scheme in _ADMITTED:
        return None
    if sep:
        try:
            parts = urlsplit(url)
            if parts.port == 0:
                return _R_SHAPE
        except ValueError:
            return _R_SHAPE
        if scheme == "https":
            if parts.username is not None or parts.password is not None or "@" in parts.netloc:
                return _R_USERINFO
            if parts.query or parts.fragment or "?" in url or "#" in url:
                return _R_SHAPE
            return None if parts.hostname else _R_SHAPE
        if scheme == "ssh":
            if parts.password is not None:
                return _R_PASSWORD
            if not parts.hostname:
                return _R_SHAPE
            return _R_OPTION if parts.hostname.startswith("-") or (parts.username or "").startswith("-") else None
        if scheme == "file":
            return _R_LOCAL
        if scheme == "http":
            return _R_HTTP
        if scheme == "git":
            return _R_GIT
        return _R_SCHEME
    if _HELPER_REMOTE.match(url):
        return _R_SCHEME
    if url.startswith("[") or "@[" in url.split("/", 1)[0]:
        return _R_BRACKETED
    match = _SCP_REMOTE.fullmatch(url)
    if match is None or url.startswith(("/", ".", "~")):
        return _R_LOCAL
    if match[1].startswith("-") or match[2].startswith("-") or match[3].startswith("-"):
        return _R_OPTION
    return None
