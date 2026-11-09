"""What a server that listens beyond this machine presents: ``collimate
--listen ADDR``.

The default server binds loopback and speaks plain HTTP, open to this
machine's own clients. ``--listen`` binds a LAN address instead, and then
two things are always on, the way Jupyter and Syncthing serve off-loopback:

- a bearer token (:mod:`auth_middleware`): ``$COLLIMATE_TOKEN`` if set, else
  one minted into ``<data dir>/.collimate/access-token`` on first use and
  kept, so every start of that data dir requires the same token;
- TLS with a self-signed certificate, minted into this user's config
  directory on first use and kept, so every data dir this user serves from
  this machine presents the same certificate. The desktop shell pins a
  certificate per host on first connect (trust on first use), so the
  pairing printout shows its fingerprint in the form Electron shows in
  that prompt: ``sha256/`` and the base64 of the DER digest.

A plain-HTTP LAN origin is not a secure context, and the app's own pages
need one (SubtleCrypto, the clipboard), so there is no unencrypted LAN
mode. An operator who wants a certificate a browser trusts keeps the
loopback default behind a TLS reverse proxy and sets ``$COLLIMATE_TOKEN``.

Secrets on disk follow the repo's pattern (:class:`auth_middleware.CellFileCaps`,
:mod:`accounts`): created exclusively at mode 0600 without following a
planted link, inside a 0700 directory, and refused on reuse when another
account can read them.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import ipaddress
import os
import secrets
import socket
import stat
from dataclasses import dataclass
from pathlib import Path

from collimate_server.user_config import config_root

TOKEN_FILE = Path(".collimate/access-token")
GITIGNORE = Path(".collimate/.gitignore")
VALID_DAYS = 3650


class Refused(ValueError):
    """A ``--listen`` the server will not serve: a bad address, or a secret
    on disk that is not private."""


def listen_address(value: str) -> str:
    """The address ``--listen`` names: an IPv4 literal that is not loopback.
    ``0.0.0.0`` is every interface. Hostnames are refused (they resolve to
    whatever DNS says that day), IPv6 is refused (uvicorn binds ``::``
    without IPv4, and a bracketed literal is not an address the desktop
    pins), and loopback is refused because the default already serves there."""
    try:
        address = ipaddress.IPv4Address(value.strip())
    except ValueError:
        raise Refused(
            f"--listen takes an IPv4 address (0.0.0.0 for every interface, or one interface's address), not {value!r}"
        ) from None
    if address.is_loopback:
        raise Refused(f"--listen {value}: the server already listens on loopback by default; name a LAN address")
    return str(address)


def _private_file(path: Path, data: bytes) -> None:
    """Create ``path`` at mode 0600, exclusively, following no link."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def _check_private(path: Path, what: str) -> None:
    """Refuse a kept secret that is a link or that another account can read
    (ssh's rule for an unprotected private key). Mode bits mean nothing on
    Windows, so only the link check applies there."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise Refused(f"{what} {path} is a symbolic link; remove it and start again")
    if os.name == "posix" and info.st_mode & 0o077:
        raise Refused(
            f"{what} {path} is readable by other accounts (mode {stat.S_IMODE(info.st_mode):o}); "
            f"chmod 600 it and start again"
        )


def _keep_out_of_git() -> None:
    """The data dir defaults to the CWD, so the token lands wherever the
    server was started from; the marker keeps a later ``git add .`` from
    committing it."""
    if not GITIGNORE.exists():
        GITIGNORE.parent.mkdir(parents=True, exist_ok=True)
        GITIGNORE.write_text("*\n", encoding="utf-8")


@dataclass(frozen=True)
class Token:
    value: str
    source: str  # "env" or the kept file's path
    minted: bool


def access_token(env: os._Environ[str] | dict[str, str] = os.environ) -> Token:
    """The token to require, relative to the data dir (the CWD): the
    environment's, else the kept file's, minted on first use."""
    configured = env.get("COLLIMATE_TOKEN")
    if configured:
        return Token(configured, "env", False)
    if TOKEN_FILE.exists() or TOKEN_FILE.is_symlink():
        _check_private(TOKEN_FILE, "the access token")
        kept = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if kept:
            return Token(kept, str(TOKEN_FILE), False)
        TOKEN_FILE.unlink()
    minted = secrets.token_urlsafe(32)
    _private_file(TOKEN_FILE, f"{minted}\n".encode())
    _keep_out_of_git()
    return Token(minted, str(TOKEN_FILE), True)


@dataclass(frozen=True)
class Certificate:
    cert_file: Path
    key_file: Path
    fingerprint: str  # sha256/<base64 of the DER digest>, Electron's form
    minted: bool


def tls_dir() -> Path:
    return config_root() / "tls"


def certificate(listen: str) -> Certificate:
    """This user's kept self-signed certificate, minted on first use. Its
    names are the machine's hostname and ``listen`` when that is one
    address; the desktop pins the leaf fingerprint and never reads them,
    so an address change needs no new certificate."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization

    directory = tls_dir()
    cert_file, key_file = directory / "cert.pem", directory / "key.pem"
    if cert_file.exists() and (key_file.exists() or key_file.is_symlink()):
        _check_private(key_file, "the TLS private key")
        try:
            cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
        except ValueError:
            pass  # a cut-short mint: both files are replaced below
        else:
            return Certificate(cert_file, key_file, _fingerprint(cert.public_bytes(serialization.Encoding.DER)), False)

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    hostname = socket.gethostname()
    names: list[x509.GeneralName] = [x509.DNSName(hostname)]
    bound = ipaddress.IPv4Address(listen)
    if not bound.is_unspecified:
        names.append(x509.IPAddress(bound))
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Collimate")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    _private_dir(directory)
    for stale in (cert_file, key_file):
        stale.unlink(missing_ok=True)
    _private_file(key_file, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    _private_file(cert_file, cert.public_bytes(serialization.Encoding.PEM))
    return Certificate(cert_file, key_file, _fingerprint(cert.public_bytes(serialization.Encoding.DER)), True)


def _fingerprint(der: bytes) -> str:
    return "sha256/" + base64.b64encode(hashlib.sha256(der).digest()).decode("ascii")


def primary_address() -> str | None:
    """The address a LAN peer most likely reaches this machine on: the
    source address the kernel would pick for an outbound packet (none is
    sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            address = ipaddress.IPv4Address(sock.getsockname()[0])
    except (OSError, ValueError):
        return None
    return None if address.is_loopback else str(address)


def pairing_urls(listen: str, port: int) -> list[str]:
    """What to type into the desktop app. One address bound: that address.
    Every interface bound: the machine's primary address, and its hostname
    for a LAN that resolves names (the stabler choice under DHCP)."""
    if not ipaddress.IPv4Address(listen).is_unspecified:
        return [f"https://{listen}:{port}"]
    urls = []
    primary = primary_address()
    if primary:
        urls.append(f"https://{primary}:{port}")
    urls.append(f"https://{socket.gethostname()}:{port}")
    return urls


def pairing_printout(urls: list[str], token: Token, cert: Certificate, data_dir: str, show_token: bool) -> str:
    """The lines a `--listen` start prints. The token's value appears only
    when asked (stdout is a terminal): a start whose output goes to a file
    or a journal names the file that holds it instead."""
    lines = ["", f"  Collimate is listening at:  {urls[0]}"]
    for url in urls[1:]:
        lines.append(f"                              {url}")
    lines.append(f"  Data directory:             {data_dir}")
    lines.append("")
    lines.append("  Pair the desktop app (Settings → Servers → Add server):")
    lines.append(f"    URL:          {urls[0]}")
    if token.source == "env":
        held = "from $COLLIMATE_TOKEN"
    else:
        held = f"kept in {os.path.join(data_dir, token.source)}"
    if show_token:
        lines.append(f"    Token:        {token.value}")
        lines.append(f"                  ({held})")
    else:
        lines.append(f"    Token:        {held}")
    lines.append(f"    Fingerprint:  {cert.fingerprint}")
    lines.append("                  (the app shows this on first connect; trust it only if it matches)")
    if cert.minted:
        lines.append("")
        lines.append(f"  New certificate: {cert.cert_file.parent}")
        lines.append("  A desktop that paired with an earlier one refuses this server until it")
        lines.append("  removes and re-adds it.")
    lines.append("")
    return "\n".join(lines)
