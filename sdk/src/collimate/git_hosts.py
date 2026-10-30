# SPDX-License-Identifier: MIT
"""Pure facts and checks for Git accounts: the published host keys of github.com and gitlab.com,
known_hosts and OpenSSH private-key parsing, and the account address rules. No I/O, no process: the
Key manager's Save and Verify and the runs share it."""
import base64
import hashlib
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

__all__ = [
    "GIT_SERVICES",
    "KEY_TYPES",
    "PINNED_FINGERPRINTS",
    "PINNED_HOST_KEYS",
    "SK_KEY_TYPES",
    "KeyInfo",
    "KnownHost",
    "endpoint_problem",
    "fingerprint",
    "git_api_base",
    "git_base",
    "host_key_name",
    "inspect_private_key",
    "normalise_private_key",
    "parse_known_hosts",
    "pinned_known_hosts",
    "ships_host_keys",
]

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

# The host rule of the checkout record (lowercase labels, a single label allowed, no IPv6, never a leading "-").
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
