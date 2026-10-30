"""The assembled service worker derives its mirror hosts from the SDK."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import collimate
import pytest
from collimate._host import module_mirror

ROOT = Path(__file__).resolve().parents[3]
SDK = Path(collimate.__file__).resolve().parents[2]


@pytest.fixture
def assemble(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "playground/build"))
    spec = importlib.util.spec_from_file_location("build_assemble", ROOT / "playground/build/assemble.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_P9_service_worker_mirror_hosts_are_the_contract_hosts(assemble, tmp_path):
    hosts = assemble.mirror_hosts(SDK)
    assert hosts == list(module_mirror.HOSTS)
    target = tmp_path / "sw.js"
    assemble.write_service_worker(ROOT / "playground/web/sw.js", target, hosts)
    text = target.read_text(encoding="utf-8")
    assert assemble.MIRROR_HOSTS_PLACEHOLDER not in text
    assert text.count(f"const MIRROR_HOSTS = new Set({json.dumps(hosts)});") == 1


@pytest.mark.parametrize("upstream", ["http://esm.sh/", "https://esm.sh", "https://esm.sh/v1/", "https:///"])
def test_P9_mirror_upstream_that_is_not_a_bare_https_host_is_refused(assemble, tmp_path, upstream):
    contract = tmp_path / "src/collimate/module_mirror_contract.json"
    contract.parent.mkdir(parents=True)
    contract.write_text(json.dumps({"upstreams": ["https://cdn.jsdelivr.net/", upstream]}), encoding="utf-8")
    with pytest.raises(SystemExit, match="is not https://<host>/"):
        assemble.mirror_hosts(tmp_path)


@pytest.mark.parametrize("body", ["const MIRROR_HOSTS = new Set([]);", "__MIRROR_HOSTS__ __MIRROR_HOSTS__"])
def test_P9_service_worker_needs_the_placeholder_exactly_once(assemble, tmp_path, body):
    source = tmp_path / "sw.js"
    source.write_text(body, encoding="utf-8")
    with pytest.raises(SystemExit, match="exactly once"):
        assemble.write_service_worker(source, tmp_path / "out.js", ["esm.sh"])
