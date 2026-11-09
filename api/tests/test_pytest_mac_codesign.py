"""A macOS server environment is signed before it is hashed
(build/mac_codesign.py), as electron-builder would sign it, and electron-builder
then leaves it alone.

No test here runs codesign or security: the commands are recorded by a fake
``run`` and checked against what electron-builder's signer runs for a file
inside the app (app-builder-lib's MacTargetHelper, @electron/osx-sign). The
real signing runs on release.yml's mac legs.
"""
from __future__ import annotations

import base64
import importlib.util
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHA = "0123456789ABCDEF0123456789ABCDEF01234567"
OTHER = "FEDCBA9876543210FEDCBA9876543210FEDCBA98"
NAME = "Developer ID Application: Collimate Contributors (TEAM123456)"
LISTING = (f'  1) {OTHER} "Apple Development: someone@example.com (ABCDE12345)"\n'
           f'  2) {SHA} "{NAME}"\n'
           f'  3) {OTHER} "Developer ID Application: Someone Else (ZZZZZZZZZZ)"\n'
           "     3 valid identities found\n")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "build" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mac_codesign = _load("mac_codesign")


class Recorder:
    """A ``subprocess.run`` that records each command and answers ``security``
    as a Mac would; ``fail`` names a command word that exits 1."""

    def __init__(self, listing: str = LISTING, keychains: str = '    "/Users/runner/Library/Keychains/login.keychain-db"\n',
                 fail: str | None = None):
        self.commands: list[list[str]] = []
        self.listing, self.keychains, self.fail = listing, keychains, fail

    def __call__(self, command, check=False, capture_output=False, text=False):
        self.commands.append(list(command))
        if self.fail and self.fail in command:
            if check:
                raise subprocess.CalledProcessError(1, command, "", f"{self.fail}: refused")
            return subprocess.CompletedProcess(command, 1, "", "")
        out = ""
        if command[:2] == ["security", "find-identity"]:
            out = self.listing
        elif command[:2] == ["security", "list-keychains"] and "-s" not in command:
            out = self.keychains
        return subprocess.CompletedProcess(command, 0, out, "")


def _tree(root: Path) -> Path:
    """A darwin environment's shape: Mach-O files (thin and universal) among
    scripts, bytecode, data and a symlink."""
    for rel, head in (("bin/python3.13", b"\xcf\xfa\xed\xfe"), ("bin/uv", b"\xcf\xfa\xed\xfe"),
                      ("lib/libpython3.13.dylib", b"\xcf\xfa\xed\xfe"),
                      ("lib/python3.13/lib-dynload/_ssl.cpython-313-darwin.so", b"\xca\xfe\xba\xbe"),
                      ("lib/python3.13/site-packages/pydantic_core/_pydantic_core.cpython-313-darwin.so",
                       b"\xcf\xfa\xed\xfe"),
                      ("lib/python3.13/json/__init__.py", b"import x"),
                      ("lib/python3.13/json/__pycache__/__init__.cpython-313.pyc", b"\xf3\r\r\n"),
                      ("lib/python3.13/config-3.13-darwin/libpython3.13.a", b"!<arch>\n")):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(head + b" body")
    (root / "bin" / "python3").symlink_to("python3.13")
    return root


MACH_O = ["bin/python3.13", "bin/uv", "lib/libpython3.13.dylib",
          "lib/python3.13/lib-dynload/_ssl.cpython-313-darwin.so",
          "lib/python3.13/site-packages/pydantic_core/_pydantic_core.cpython-313-darwin.so"]


def test_the_mach_o_files_are_found_by_their_magic_number(tmp_path):
    assert mac_codesign.mach_o_files(_tree(tmp_path / "server")) == MACH_O


def test_the_files_are_signed_deepest_first_as_osx_sign_orders_them():
    assert mac_codesign.signing_order(MACH_O) == [
        "lib/python3.13/site-packages/pydantic_core/_pydantic_core.cpython-313-darwin.so",
        "lib/python3.13/lib-dynload/_ssl.cpython-313-darwin.so",
        "bin/python3.13", "bin/uv", "lib/libpython3.13.dylib"]


def test_the_codesign_command_is_electron_builders_for_a_file_in_the_app(tmp_path):
    """osx-sign: ``--sign <identity> --force [--keychain K] --timestamp
    [--options runtime] --entitlements E <file>``. Ad-hoc has the unsigned
    config's hardened runtime (off) and no timestamp, which it cannot have."""
    path = tmp_path / "bin" / "python3.13"
    entitlements = str(mac_codesign.ENTITLEMENTS)
    assert mac_codesign.codesign_command(mac_codesign.Signer(), path) == [
        "codesign", "--sign", "-", "--force", "--timestamp=none", "--entitlements", entitlements, str(path)]
    keychain = tmp_path / "k.keychain"
    assert mac_codesign.codesign_command(mac_codesign.Signer(SHA, NAME, keychain), path) == [
        "codesign", "--sign", SHA, "--force", "--keychain", str(keychain), "--timestamp", "--options", "runtime",
        "--entitlements", entitlements, str(path)]
    assert mac_codesign.codesign_command(mac_codesign.Signer(SHA, NAME), path)[:5] == [
        "codesign", "--sign", SHA, "--force", "--timestamp"]


def test_the_entitlements_are_electron_builders_default_for_a_file_in_the_app():
    entitlements = plistlib.loads(mac_codesign.ENTITLEMENTS.read_bytes())
    assert entitlements == {"com.apple.security.cs.allow-jit": True,
                            "com.apple.security.cs.allow-unsigned-executable-memory": True,
                            "com.apple.security.cs.disable-library-validation": True}
    template = ROOT / "desktop/node_modules/app-builder-lib/templates/entitlements.mac.plist"
    if template.is_file():
        assert plistlib.loads(template.read_bytes()) == entitlements


def test_every_mach_o_file_is_signed_then_verified_and_nothing_else(tmp_path):
    tree = _tree(tmp_path / "server")
    run = Recorder()
    signer = mac_codesign.Signer(SHA, NAME)
    assert mac_codesign.sign_tree(tree, signer, run) == MACH_O
    signs = [c for c in run.commands if c[1] == "--sign"]
    verifies = [c for c in run.commands if c[1] == "--verify"]
    assert run.commands == signs + verifies
    assert [c[-1] for c in signs] == [str(tree / rel) for rel in mac_codesign.signing_order(MACH_O)]
    assert all(c == mac_codesign.codesign_command(signer, Path(c[-1])) for c in signs)
    assert verifies == [["codesign", "--verify", "--strict", str(tree / rel)] for rel in MACH_O]


def test_a_tree_that_cannot_be_signed_file_by_file_is_refused(tmp_path):
    tree = _tree(tmp_path / "server")
    (tree / "lib" / "Python.framework").mkdir()
    with pytest.raises(mac_codesign.Refused, match="code bundle"):
        mac_codesign.sign_tree(tree, mac_codesign.Signer(), Recorder())
    empty = tmp_path / "empty"
    (empty / "bin").mkdir(parents=True)
    with pytest.raises(mac_codesign.Refused, match="no Mach-O"):
        mac_codesign.sign_tree(empty, mac_codesign.Signer(), Recorder())


def test_a_signing_failure_is_refused_with_what_codesign_said(tmp_path):
    tree = _tree(tmp_path / "server")
    with pytest.raises(mac_codesign.Refused, match="codesign --sign - .* failed \\(1\\): --force: refused"):
        mac_codesign.sign_tree(tree, mac_codesign.Signer(), Recorder(fail="--force"))


@pytest.mark.parametrize(("env", "release", "signs"), [
    ({"MAC_SIGNING": "true"}, True, True), ({"MAC_SIGNING": "true"}, False, False),
    ({"MAC_SIGNING": "false"}, True, False), ({}, True, False)])
def test_only_a_release_with_mac_signing_signs_with_the_developer_id(env, release, signs):
    assert mac_codesign.release_signs(env, release) is signs
    if not signs:
        with mac_codesign.signer_for(env, release) as signer:
            assert signer == mac_codesign.Signer() and signer.manifest() == {
                "identity": "-", "name": None, "hardened_runtime": False}


def test_a_flag_that_is_not_true_or_false_is_refused():
    with pytest.raises(mac_codesign.Refused, match="MAC_SIGNING"):
        mac_codesign.release_signs({"MAC_SIGNING": "1"}, True)


def test_the_certificate_is_read_from_csc_link_as_electron_builder_reads_it(tmp_path, monkeypatch):
    p12 = b"\x30\x82 a pkcs12 blob"
    encoded = base64.b64encode(p12).decode()
    assert encoded.endswith("=")
    # A data: URI, base64 ending in padding, and base64 longer than 2048 characters.
    for link in (f"data:application/x-pkcs12;base64,{encoded}", encoded,
                 base64.b64encode(p12 + b"x" * 2000).decode()):
        written = mac_codesign.certificate(link, tmp_path)
        assert written.read_bytes().startswith(p12)
    given = tmp_path / "cert.p12"
    given.write_bytes(p12)
    assert mac_codesign.certificate(str(given), tmp_path) == given
    assert mac_codesign.certificate(f"file://{given}", tmp_path) == given
    monkeypatch.setattr(mac_codesign, "ROOT", tmp_path)
    (tmp_path / "desktop").mkdir()
    (tmp_path / "desktop" / "local.p12").write_bytes(p12)
    assert mac_codesign.certificate("local.p12", tmp_path) == tmp_path / "desktop" / "local.p12"
    with pytest.raises(mac_codesign.Refused, match="URL"):
        mac_codesign.certificate("https://example.com/cert.p12", tmp_path)
    with pytest.raises(mac_codesign.Refused, match="not a file"):
        mac_codesign.certificate(str(tmp_path / "missing.p12"), tmp_path)


def test_the_identity_is_the_first_developer_id_application_one(tmp_path):
    assert mac_codesign.find_identity(LISTING, None) == (SHA, NAME)
    assert mac_codesign.find_identity(LISTING, "Someone Else")[0] == OTHER
    assert mac_codesign.find_identity(LISTING, SHA) == (SHA, NAME)
    for listing, name in (("     0 valid identities found\n", None), (LISTING, "Nobody")):
        with pytest.raises(mac_codesign.Refused, match="no valid Developer ID Application identity"):
            mac_codesign.find_identity(listing, name)


def test_csc_link_is_imported_into_a_keychain_of_its_own_and_removed_after(tmp_path):
    """electron-builder's createKeychain: a keychain with a random password,
    unlocked, on the user's search list, the certificate imported for
    codesign with the partition list set; the identity searched there alone.
    After the block the search list is the one before and the keychain gone,
    also when the block fails."""
    given = tmp_path / "cert.p12"
    given.write_bytes(b"p12")
    env = {"CSC_LINK": str(given), "CSC_KEY_PASSWORD": "certificate password"}
    run = Recorder()
    with mac_codesign.developer_id(env, run) as signer:
        assert signer.identity == SHA and signer.name == NAME and signer.developer_id
        keychain = signer.keychain
        assert keychain is not None and keychain.name == "server-env.keychain"
        during = list(run.commands)
    words = [c[1] for c in during]
    assert words == ["list-keychains", "create-keychain", "unlock-keychain", "set-keychain-settings",
                     "list-keychains", "import", "set-key-partition-list", "find-identity"]
    password = during[1][3]
    assert during[1] == ["security", "create-keychain", "-p", password, str(keychain)] and len(password) >= 32
    assert during[2] == ["security", "unlock-keychain", "-p", password, str(keychain)]
    assert during[4] == ["security", "list-keychains", "-d", "user", "-s", str(keychain),
                         "/Users/runner/Library/Keychains/login.keychain-db"]
    assert during[5] == ["security", "import", str(given), "-k", str(keychain), "-T", "/usr/bin/codesign",
                         "-P", "certificate password"]
    assert during[6] == ["security", "set-key-partition-list", "-S", "apple-tool:,apple:", "-s", "-k", password,
                         str(keychain)]
    assert during[7] == ["security", "find-identity", "-v", "-p", "codesigning", str(keychain)]
    assert run.commands[len(during):] == [
        ["security", "list-keychains", "-d", "user", "-s", "/Users/runner/Library/Keychains/login.keychain-db"],
        ["security", "delete-keychain", str(keychain)]]
    assert not keychain.parent.exists()

    failing = Recorder()
    with pytest.raises(RuntimeError):
        with mac_codesign.developer_id(env, failing):
            raise RuntimeError("signing failed")
    assert [c[1] for c in failing.commands[-2:]] == ["list-keychains", "delete-keychain"]


def test_a_keychain_failure_names_the_command_but_no_password(tmp_path):
    given = tmp_path / "cert.p12"
    given.write_bytes(b"p12")
    run = Recorder(fail="import")
    with pytest.raises(mac_codesign.Refused) as refused:
        with mac_codesign.developer_id({"CSC_LINK": str(given), "CSC_KEY_PASSWORD": "hunter2"}, run):
            pass
    assert "security import" in str(refused.value) and "hunter2" not in str(refused.value)
    assert [c[1] for c in run.commands[-2:]] == ["list-keychains", "delete-keychain"]


def test_without_csc_link_the_identity_is_found_on_the_search_list():
    run = Recorder()
    with mac_codesign.developer_id({"CSC_NAME": "Collimate Contributors"}, run) as signer:
        assert signer == mac_codesign.Signer(SHA, NAME)
    assert run.commands == [["security", "find-identity", "-v", "-p", "codesigning"]]
    assert signer.manifest() == {"identity": SHA, "name": NAME, "hardened_runtime": True}
