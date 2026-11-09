"""`collimate --listen`: the kept token and certificate, what the CLI binds
and prints, and what it refuses."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import socket
import stat
import sys

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

import collimate_server.listen_identity as listen_identity


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A data dir as the CWD and a per-user config root, both this test's."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.chdir(data)
    config = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(listen_identity, "config_root", lambda: config / "collimate")
    monkeypatch.delenv("COLLIMATE_TOKEN", raising=False)
    return data


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


# --- the address ----------------------------------------------------------

@pytest.mark.parametrize("value, kept", [("0.0.0.0", "0.0.0.0"), (" 192.168.1.5 ", "192.168.1.5"), ("10.0.0.1", "10.0.0.1")])
def test_listen_accepts_a_lan_ipv4_address(value, kept):
    assert listen_identity.listen_address(value) == kept


@pytest.mark.parametrize("value, why", [
    ("127.0.0.1", "already listens on loopback"),
    ("127.1.2.3", "already listens on loopback"),
    ("::", "IPv4 address"),
    ("fe80::1", "IPv4 address"),
    ("nas.local", "IPv4 address"),
    ("", "IPv4 address"),
])
def test_listen_refuses_loopback_ipv6_and_names(value, why):
    with pytest.raises(listen_identity.Refused, match=why):
        listen_identity.listen_address(value)


# --- the token ---------------------------------------------------------------

def test_token_is_minted_privately_once_and_kept(home):
    first = listen_identity.access_token()
    assert first.minted and first.source == ".collimate/access-token"
    assert len(first.value) >= 43
    token_file = home / ".collimate" / "access-token"
    assert token_file.read_text() == f"{first.value}\n"
    if os.name == "posix":
        assert mode(token_file) == 0o600
    assert (home / ".collimate" / ".gitignore").read_text() == "*\n"
    second = listen_identity.access_token()
    assert second == listen_identity.Token(first.value, ".collimate/access-token", False)


def test_a_data_dir_with_its_own_gitignore_keeps_it(home):
    (home / ".collimate").mkdir()
    (home / ".collimate" / ".gitignore").write_text("custom\n")
    token = listen_identity.access_token()
    assert token.minted
    assert (home / ".collimate" / ".gitignore").read_text() == "custom\n"
    if os.name == "posix":
        assert mode(home / ".collimate" / "access-token") == 0o600


def test_environment_token_wins_and_mints_nothing(home, monkeypatch):
    monkeypatch.setenv("COLLIMATE_TOKEN", "from-the-environment")
    assert listen_identity.access_token() == listen_identity.Token("from-the-environment", "env", False)
    assert not (home / ".collimate").exists()


def test_an_empty_token_file_is_replaced(home):
    (home / ".collimate").mkdir()
    (home / ".collimate" / "access-token").write_text("\n")
    (home / ".collimate" / "access-token").chmod(0o600)
    token = listen_identity.access_token()
    assert token.minted and (home / ".collimate" / "access-token").read_text().strip() == token.value


@pytest.mark.skipif(os.name != "posix", reason="mode bits")
def test_a_token_file_other_accounts_can_read_is_refused(home):
    (home / ".collimate").mkdir()
    token_file = home / ".collimate" / "access-token"
    token_file.write_text("kept\n")
    token_file.chmod(0o644)
    with pytest.raises(listen_identity.Refused, match="readable by other accounts .*mode 644.*chmod 600"):
        listen_identity.access_token()
    assert token_file.read_text() == "kept\n"


def test_a_link_at_the_token_path_is_not_followed(home, tmp_path):
    (home / ".collimate").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("planted\n")
    (home / ".collimate" / "access-token").symlink_to(elsewhere)
    with pytest.raises(listen_identity.Refused, match="symbolic link"):
        listen_identity.access_token()
    assert elsewhere.read_text() == "planted\n"


# --- the certificate ----------------------------------------------------------

def electron_fingerprint(cert_file) -> str:
    der = x509.load_pem_x509_certificate(cert_file.read_bytes()).public_bytes(serialization.Encoding.DER)
    return "sha256/" + base64.b64encode(hashlib.sha256(der).digest()).decode()


def test_certificate_is_minted_once_per_user_with_electron_fingerprint(home, tmp_path):
    first = listen_identity.certificate("192.168.1.5")
    assert first.minted
    assert first.cert_file == tmp_path / "config" / "collimate" / "tls" / "cert.pem"
    assert first.key_file == first.cert_file.with_name("key.pem")
    assert first.fingerprint == electron_fingerprint(first.cert_file)
    if os.name == "posix":
        assert mode(first.cert_file.parent) == 0o700
        assert mode(first.key_file) == 0o600
    cert = x509.load_pem_x509_certificate(first.cert_file.read_bytes())
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == [socket.gethostname()]
    assert names.get_values_for_type(x509.IPAddress) == [ipaddress.IPv4Address("192.168.1.5")]
    assert cert.subject.rfc4514_string() == "CN=Collimate"
    assert (cert.not_valid_after_utc - cert.not_valid_before_utc).days == listen_identity.VALID_DAYS + 1
    # The private key never leaves this file: PKCS#8, unencrypted, loadable.
    serialization.load_pem_private_key(first.key_file.read_bytes(), password=None)

    # A second data dir of the same user, and a different address, keep it.
    other = tmp_path / "other"
    other.mkdir()
    os.chdir(other)
    second = listen_identity.certificate("0.0.0.0")
    assert second == listen_identity.Certificate(first.cert_file, first.key_file, first.fingerprint, False)


def test_every_interface_names_only_the_hostname(home):
    cert = x509.load_pem_x509_certificate(listen_identity.certificate("0.0.0.0").cert_file.read_bytes())
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.IPAddress) == []
    assert names.get_values_for_type(x509.DNSName) == [socket.gethostname()]


@pytest.mark.skipif(os.name != "posix", reason="mode bits")
def test_a_key_other_accounts_can_read_is_refused(home):
    kept = listen_identity.certificate("0.0.0.0")
    kept.key_file.chmod(0o640)
    with pytest.raises(listen_identity.Refused, match="TLS private key .*readable by other accounts"):
        listen_identity.certificate("0.0.0.0")


def test_a_cut_short_certificate_file_is_replaced(home):
    kept = listen_identity.certificate("0.0.0.0")
    kept.cert_file.write_bytes(b"")
    fresh = listen_identity.certificate("0.0.0.0")
    assert fresh.minted and fresh.fingerprint == electron_fingerprint(fresh.cert_file)
    serialization.load_pem_private_key(fresh.key_file.read_bytes(), password=None)


def test_a_certificate_without_its_key_is_replaced(home):
    kept = listen_identity.certificate("0.0.0.0")
    kept.key_file.unlink()
    fresh = listen_identity.certificate("0.0.0.0")
    assert fresh.minted and fresh.fingerprint != kept.fingerprint


# --- the pairing lines --------------------------------------------------------

def test_pairing_urls(monkeypatch):
    assert listen_identity.pairing_urls("192.168.1.5", 5001) == ["https://192.168.1.5:5001"]
    monkeypatch.setattr(listen_identity, "primary_address", lambda: "10.1.2.3")
    monkeypatch.setattr(socket, "gethostname", lambda: "nas")
    assert listen_identity.pairing_urls("0.0.0.0", 8443) == ["https://10.1.2.3:8443", "https://nas:8443"]
    monkeypatch.setattr(listen_identity, "primary_address", lambda: None)
    assert listen_identity.pairing_urls("0.0.0.0", 8443) == ["https://nas:8443"]


def test_primary_address_is_never_loopback():
    found = listen_identity.primary_address()
    assert found is None or not ipaddress.IPv4Address(found).is_loopback


def printout(show_token: bool, minted_cert: bool = False, source: str = ".collimate/access-token") -> str:
    token = listen_identity.Token("synthetic-token-value", source, False)
    cert = listen_identity.Certificate(
        listen_identity.tls_dir() / "cert.pem", listen_identity.tls_dir() / "key.pem", "sha256/abc=", minted_cert)
    return listen_identity.pairing_printout(
        ["https://10.1.2.3:5001", "https://nas:5001"], token, cert, "/srv/collimate", show_token=show_token)


def test_printout_on_a_terminal_shows_the_token_and_where_it_is_kept(home):
    text = printout(show_token=True)
    assert "  Collimate is listening at:  https://10.1.2.3:5001\n                              https://nas:5001\n" in text
    assert "    URL:          https://10.1.2.3:5001\n" in text
    assert "    Token:        synthetic-token-value\n                  (kept in /srv/collimate/.collimate/access-token)\n" in text
    assert "    Fingerprint:  sha256/abc=\n" in text
    assert "trust it only if it matches" in text
    assert "New certificate" not in text


def test_printout_to_a_file_names_the_token_file_instead_of_the_token(home):
    text = printout(show_token=False)
    assert "synthetic-token-value" not in text
    assert "    Token:        kept in /srv/collimate/.collimate/access-token\n" in text
    assert "from $COLLIMATE_TOKEN" in printout(show_token=False, source="env")
    assert "synthetic-token-value" not in printout(show_token=False, source="env")


def test_printout_warns_paired_desktops_about_a_new_certificate(home):
    text = printout(show_token=False, minted_cert=True)
    assert f"  New certificate: {listen_identity.tls_dir()}\n" in text
    assert "removes and re-adds it" in text


# --- the CLI -------------------------------------------------------------------

def run_cli(monkeypatch, argv: list[str], capsys):
    import collimate_server.main as main
    calls: list[dict] = []

    def serve(app, announce, **kwargs):
        # A start that bound its socket: the printout follows.
        calls.append(kwargs)
        announce()

    monkeypatch.setattr(main, "_serve_announcing", serve)
    monkeypatch.setattr(sys, "argv", ["collimate", *argv])
    main.cli_main()
    return calls, capsys.readouterr().out


def test_cli_listen_binds_tls_requires_the_kept_token_and_prints_pairing(home, monkeypatch, capsys):
    import collimate_server.main as main
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    calls, out = run_cli(monkeypatch, ["--listen", "0.0.0.0", "--port", "8443", "--data-dir", str(home)], capsys)
    cert = listen_identity.certificate("0.0.0.0")
    assert calls == [{"host": "0.0.0.0", "port": 8443, "ws_ping_timeout": 40.0,
                      "ssl_certfile": str(cert.cert_file), "ssl_keyfile": str(cert.key_file)}]
    token = (home / ".collimate" / "access-token").read_text().strip()
    assert main._auth.enabled and main._auth.matches(token)
    assert token not in out
    assert f"kept in {home / '.collimate' / 'access-token'}" in out
    assert cert.fingerprint in out
    assert ":8443" in out and "https://" in out and "http://localhost" not in out


def test_cli_listen_shows_the_token_on_a_terminal(home, monkeypatch, capsys):
    monkeypatch.setenv("COLLIMATE_TOKEN", "synthetic-terminal-token")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    calls, out = run_cli(monkeypatch, ["--listen", "192.168.1.5", "--data-dir", str(home)], capsys)
    assert calls[0]["host"] == "192.168.1.5" and calls[0]["port"] == 5001
    assert "    Token:        synthetic-terminal-token\n                  (from $COLLIMATE_TOKEN)\n" in out
    assert "    URL:          https://192.168.1.5:5001\n" in out
    assert not (home / ".collimate" / "access-token").exists()


@pytest.mark.parametrize("value", ["127.0.0.1", "::", "nas.local"])
def test_cli_refuses_a_bad_listen_address_before_binding(home, monkeypatch, capsys, value):
    import collimate_server.main as main
    monkeypatch.setattr(main, "_serve_announcing", lambda *a, **k: pytest.fail("bound anyway"))
    monkeypatch.setattr(sys, "argv", ["collimate", "--listen", value, "--data-dir", str(home)])
    with pytest.raises(SystemExit) as exit_info:
        main.cli_main()
    assert exit_info.value.code == 2
    assert "--listen" in capsys.readouterr().err
    assert not (home / ".collimate" / "access-token").exists()


def test_cli_default_stays_loopback_http_and_open(home, monkeypatch, capsys):
    import collimate_server.main as main
    calls, out = run_cli(monkeypatch, ["--data-dir", str(home)], capsys)
    assert calls == [{"host": "127.0.0.1", "port": 5001, "ws_ping_timeout": 40.0}]
    assert not main._auth.enabled
    assert "http://localhost:5001" in out and "collimate --listen 0.0.0.0" in out
    assert "--token" not in out
    assert not (home / ".collimate" / "access-token").exists()


def test_cli_has_no_token_flag(home, monkeypatch, capsys):
    import collimate_server.main as main
    monkeypatch.setattr(sys, "argv", ["collimate", "--token", "on-argv", "--data-dir", str(home)])
    with pytest.raises(SystemExit):
        main.cli_main()
    assert "unrecognized arguments: --token" in capsys.readouterr().err
