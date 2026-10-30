# SPDX-License-Identifier: MIT
"""The run's Git account: the optional ``git`` role, a Key manager GitHub,
GitLab or Git server entry (kind git) with its commit author. Every Git
consumer (the Git engine, the Agent's harness, Launch, release lookups) reads
it here, so one rule decides which host may receive the credential: no role
bound is anonymous HTTPS, and a bound entry that cannot serve is refused,
never silently ignored."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Literal
from urllib.parse import quote, urlsplit

from collimate import git_hosts
from collimate.git_hosts import KnownHost

from _env_shared.git_run import HttpsAuth, Identity, Remote, SshAuth, basic_authorization

Service = Literal["github", "gitlab", "git"]
Auth = Literal["api_key", "ssh_key", "basic", "none"]
ROLE = "git"

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
_NO_ANONYMOUS = "[upstream] GitHub release lookups are anonymous: {why}"
_NOT_GITHUB_COM = "the Git account is not a github.com token"


class RemoteRefused(ValueError):
    """The account cannot serve this address. ``fields`` hold only validated
    values (base, host, hostport, method, scheme, other) for the message."""

    def __init__(self, code: Literal["scope", "transport", "no_host_key"], **fields: str) -> None:
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
    known_hosts: tuple[KnownHost, ...] = ()    # ssh_key: the pinned keys plus the account's own lines
    identity: Identity = field(default_factory=lambda: Identity("", ""))
    own_known_hosts: tuple[KnownHost, ...] = ()  # ssh_key: the lines pasted in the account's Known hosts

    @property
    def transport(self) -> Literal["https", "ssh"]:
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

    def secret_forms(self) -> tuple[str, ...]:
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

    def agent_known_hosts(self) -> tuple[KnownHost, ...]:
        """The host keys an agent's ssh may trust, so its key goes only where
        :meth:`auth_for` would send it: on a forge, the keys (shipped or the
        account's own) of its web host on port 22, under that name alone; on
        a Git server, the account's own lines."""
        if not self.forge:
            return self.own_known_hosts
        host = self._web_address().host
        return tuple(dict.fromkeys(KnownHost((host,), known.keytype, known.blob)
                                   for known in self.known_hosts if host in known.hosts))

    def ssh_rewrite(self) -> "tuple[str, str] | None":
        """``(ssh base, https base)`` for a forge account with an SSH key: an
        https address under the web base is reached as
        ``ssh://git@<web host>/<project>`` (git_run.resolve_remote), the base's
        port and path dropped. None for any other account."""
        if not (self.forge and self.auth == "ssh_key"):
            return None
        return f"ssh://git@{self._web_address().host}/", self.web + "/"

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

    def compare_url(self, remote: str, base: str, head: str) -> "str | None":
        """The web page comparing ``head`` with ``base`` on a forge; None on a Git server."""
        if not self.forge or not remote.startswith("https://"):
            return None
        page = remote.removesuffix(".git")
        span = f"{quote(base, safe='/')}...{quote(head, safe='/')}"
        if self.service == "github":
            return f"{page}/compare/{span}?expand=1"
        return f"{page}/-/compare/{span}"

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


def git_account(role: str = ROLE) -> "GitAccount | None":
    """The bound Git account; None when the run binds none. Raises
    ``ValueError`` with the fix (G1–G6) when the bound entry cannot serve.
    Never logs a snapshot field."""
    import gen

    try:
        account = gen.account(role)
    except LookupError:
        return None
    if not isinstance(account, dict):
        raise ValueError(G1)
    service, auth = account.get("service"), account.get("auth_type")
    if (account.get("kind") != "git" or service not in git_hosts.GIT_SERVICES
            or auth not in _AUTHS.get(service, ())):
        raise ValueError(G1)
    meta = account.get("metadata") if isinstance(account.get("metadata"), dict) else {}
    identity = _author(meta)
    secrets = account.get("secrets") if isinstance(account.get("secrets"), dict) else {}
    endpoint = str(account.get("endpoint") or "")
    problem = git_hosts.endpoint_problem(service, auth, endpoint)
    if problem is not None:
        raise ValueError(G4.format(problem=problem))
    if service in _FORGE_WEB:
        web = git_hosts.git_base(endpoint or _FORGE_WEB[service])
    elif auth == "basic":
        if not endpoint.strip():
            raise ValueError(G4.format(problem="no server URL"))
        web = git_hosts.git_base(endpoint)
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
        secret = git_hosts.normalise_private_key(key)
        try:
            git_hosts.inspect_private_key(secret)
        except ValueError as exc:
            raise ValueError(G5.format(problem=exc)) from None
        try:
            own = git_hosts.parse_known_hosts(str(meta.get("known_hosts") or ""))
        except ValueError as exc:
            raise ValueError(G6.format(problem=exc)) from None
        known = git_hosts.pinned_known_hosts() + own
    return GitAccount(service, auth, web, username, secret, known, identity, own)


def release_token(log: "Callable[[str], None] | None" = None) -> "str | None":
    """The token for GitHub release lookups: the Git account's secret only
    when it is a github.com token; anonymous (logged through ``log``)
    otherwise, never failing the run. Never raises."""
    try:
        account = git_account()
    except ValueError as exc:
        if log is not None:
            log(_NO_ANONYMOUS.format(why=exc))
        return None
    if account is None:
        return None
    if account.service == "github" and account.auth == "api_key" and account.web == "https://github.com":
        return account.secret
    if log is not None:
        log(_NO_ANONYMOUS.format(why=_NOT_GITHUB_COM))
    return None
