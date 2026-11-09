"""The Collimate-built Docker images: inventory, reclaim, preview and delete
(``docker_inventory``, ``image_uses``, ``routes/storage.py``'s docker routes;
plan 1105 Phase 3).

Against a fake daemon whose images share layers: a selection's exact reclaim
is the layers only it references; a daemon whose sizes do not map to layers
falls back to ``sum(unique_size)`` marked inexact; images with a container
or a tag outside ``collimate-*`` are never candidates and are skipped at
delete; a preview expires and is used once; a delete re-checks each image.
The live test at the end builds two layered tags on the real daemon and
deletes the unused one.
"""
from __future__ import annotations

import hashlib
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

import pytest

MB = 1_000_000


def _diff(name: str) -> str:
    return "sha256:" + hashlib.sha256(name.encode()).hexdigest()


class _Image:
    def __init__(self, image_id, tags, layers, *, empty_entries=1, created=1_793_000_000):
        self.id = image_id
        self.tags = list(tags)
        self.layers = list(layers)  # [(name, bytes)], bottom first
        self.empty_entries = empty_entries
        self.created = created


class _API:
    def __init__(self, daemon):
        self._daemon = daemon

    def containers(self, all=False):
        assert all
        return [{"Image": image_id, "ImageID": image_id} for image_id in self._daemon.containers]

    def inspect_image(self, image_id):
        import docker.errors
        image = self._daemon.images.get(image_id)
        if image is None:
            raise docker.errors.NotFound(f"no such image: {image_id}")
        return {"Id": image.id, "RepoTags": list(image.tags),
                "RootFS": {"Type": "layers", "Layers": [_diff(name) for name, _ in image.layers]}}

    def history(self, image_id):
        image = self._daemon.images[image_id]
        entries = [{"Size": size, "CreatedBy": f"RUN {name}"} for name, size in image.layers]
        entries += [{"Size": 0, "CreatedBy": "ENV X=1"}] * image.empty_entries
        return list(reversed(entries))


class _ImagesAPI:
    def __init__(self, daemon):
        self._daemon = daemon
        self.removed: list[tuple[str, bool, bool]] = []

    def remove(self, image, force=False, noprune=False):
        import docker.errors
        self.removed.append((image, force, noprune))
        owner = next(i for i in self._daemon.images.values() if image in i.tags)
        if len(owner.tags) == 1 and owner.id in self._daemon.containers:
            raise docker.errors.APIError("conflict", explanation="image is being used by a container")
        if image in self._daemon.refuse:
            raise docker.errors.APIError("conflict", explanation=f"refused {image}")
        owner.tags.remove(image)
        if not owner.tags:
            del self._daemon.images[owner.id]


class FakeDaemon:
    """A daemon with layer sharing, sized as the overlay2 store sizes images
    (``Size`` = the sum of the layers); ``extra`` bytes per image model the
    containerd store, whose ``Size`` also counts compressed content."""

    def __init__(self, images, containers=(), *, extra=0, build_cache=()):
        self.images = {image.id: image for image in images}
        self.containers = list(containers)
        self.extra = extra
        self.build_cache = list(build_cache)
        self.refuse: set[str] = set()
        self.api = _API(self)
        self.images_api = _ImagesAPI(self)
        self.closed = False

    def _chains(self, image):
        from collimate_server.docker_inventory import chain_ids
        return list(zip(chain_ids([_diff(name) for name, _ in image.layers]), [s for _, s in image.layers]))

    def df(self):
        holders: dict[str, int] = {}
        for image in self.images.values():
            for chain, _ in self._chains(image):
                holders[chain] = holders.get(chain, 0) + 1
        rows = []
        for image in self.images.values():
            stack = self._chains(image)
            rows.append({
                "Id": image.id, "RepoTags": list(image.tags) or ["<none>:<none>"], "Created": image.created,
                "Size": sum(size for _, size in stack) + self.extra,
                "SharedSize": sum(size for chain, size in stack if holders[chain] > 1),
                "Containers": self.containers.count(image.id),
            })
        return {"Images": rows, "BuildCache": self.build_cache}

    def close(self):
        self.closed = True


def _client(daemon):
    """The fake as docker-py's client shape (``client.images.remove``)."""

    class Client:
        api = daemon.api
        images = daemon.images_api

        def df(self):
            return daemon.df()

        def close(self):
            daemon.closed = True
    return Client()


def _standard(**kwargs):
    """base (b1 100 MB, b2 50 MB) under two tools (30 MB, 20 MB), a run image
    with a container sharing b1, and a public image."""
    base = [("b1", 100 * MB), ("b2", 50 * MB)]
    return FakeDaemon([
        _Image("sha256:base", ["collimate-linux-small:aaa"], base),
        _Image("sha256:tool1", ["collimate-tool:111"], base + [("t1", 30 * MB)]),
        _Image("sha256:tool2", ["collimate-tool:222"], base + [("t2", 20 * MB)]),
        _Image("sha256:run", ["collimate-runner:py3.13-x"], [("b1", 100 * MB), ("r1", 5 * MB)]),
        _Image("sha256:python", ["python:3.13-slim"], [("p1", 10 * MB)]),
    ], containers=["sha256:run"], **kwargs)


NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def _inventory(daemon, uses=None):
    from collimate_server import docker_inventory
    return docker_inventory.read_inventory(_client(daemon), uses or {})


def _rows(inventory, *ids):
    by_id = {row.id: row for row in inventory.images}
    return [by_id[i] for i in ids]


def test_the_inventory_lists_collimate_images_with_docker_sizes_and_containers(fresh_cwd):
    daemon = _standard(build_cache=[{"Size": 7, "Shared": False}, {"Size": 100, "Shared": True}])
    inventory = _inventory(daemon)
    assert [row.id for row in inventory.images] == ["sha256:base", "sha256:run", "sha256:tool1", "sha256:tool2"]
    base, run, tool1, _ = inventory.images
    assert (base.size, base.shared_size, base.unique_size) == (150 * MB, 150 * MB, 0)
    assert (tool1.size, tool1.shared_size, tool1.unique_size) == (180 * MB, 150 * MB, 30 * MB)
    assert run.containers == 1 and tool1.containers == 0
    assert tool1.created_at == "2026-10-26T07:33:20+00:00"
    assert inventory.build_cache_bytes == 7
    assert set(inventory.daemon) == set(daemon.images)


def test_a_selections_exact_reclaim_is_the_layers_only_it_references(fresh_cwd):
    from collimate_server import docker_inventory as inv
    daemon = _standard()
    inventory = _inventory(daemon)
    client = _client(daemon)
    rows = inv.candidates(inventory, "all", None, NOW)
    assert [row.id for row in rows] == ["sha256:base", "sha256:tool1", "sha256:tool2"]
    model = inv.measure(client, inventory, rows)
    assert model is not None
    # b1 stays (the run image holds it); b2 goes with the last image on it.
    assert inv.reclaim(rows, model) == (50 * MB + 30 * MB + 20 * MB, True)
    # More than the unique sizes (0 + 30 + 20): b2 is shared only inside the selection.
    assert sum(row.unique_size for row in rows) == 50 * MB
    tools = _rows(inventory, "sha256:tool1", "sha256:tool2")
    assert inv.reclaim(tools, inv.measure(client, inventory, tools)) == (50 * MB, True)
    assert inv.reclaim([], None) == (0, True)


def test_sizes_that_do_not_map_to_layers_fall_back_to_the_unique_sum_marked_inexact(fresh_cwd):
    from collimate_server import docker_inventory as inv
    daemon = _standard(extra=3 * MB)  # the containerd store's Size also counts compressed blobs
    inventory = _inventory(daemon)
    rows = inv.candidates(inventory, "all", None, NOW)
    assert inv.measure(_client(daemon), inventory, rows) is None
    assert inv.reclaim(rows, None) == (sum(row.unique_size for row in rows), False)


def test_layer_sizes_come_from_the_layer_creating_history_entries():
    from collimate_server.docker_inventory import layer_sizes
    newest_first = [{"Size": 0}, {"Size": 5}, {"Size": 0}, {"Size": 7}]
    assert layer_sizes(newest_first, 2) == [7, 5]
    assert layer_sizes(newest_first, 4) == [7, 0, 5, 0]
    # Three layers, one of them empty: which zero entry is it? Not knowable.
    assert layer_sizes(newest_first, 3) is None


def test_an_ambiguous_history_makes_the_figure_inexact(fresh_cwd):
    from collimate_server import docker_inventory as inv
    daemon = FakeDaemon([_Image("sha256:z", ["collimate-z:1"], [("z1", 10), ("z0", 0)], empty_entries=1)])
    inventory = _inventory(daemon)
    assert inv.measure(_client(daemon), inventory, list(inventory.images)) is None


def test_images_with_a_container_or_a_foreign_tag_are_never_candidates(fresh_cwd):
    from collimate_server import docker_inventory as inv
    daemon = _standard()
    daemon.images["sha256:tool2"].tags.append("mine:latest")
    inventory = _inventory(daemon)
    (tool2,) = _rows(inventory, "sha256:tool2")
    assert tool2.refs == ("collimate-tool:222",) and tool2.other_refs == ("mine:latest",)
    for mode, days in (("all", None), ("unused", 0)):
        assert [row.id for row in inv.candidates(inventory, mode, days, NOW)] == ["sha256:base", "sha256:tool1"]


def test_unused_selects_images_with_no_use_in_the_days_or_none_recorded(fresh_cwd, monkeypatch):
    from collimate_server import docker_inventory as inv, image_uses
    daemon = _standard()
    for ref, image_id, when in (("collimate-tool:111", "sha256:tool1", NOW - timedelta(days=2)),
                                ("collimate-tool:222", "sha256:tool2", NOW - timedelta(days=40)),
                                ("collimate-linux-small:aaa", "sha256:other-image", NOW)):
        monkeypatch.setattr("collimate_server.time_wire.now_iso", lambda when=when: when.isoformat())
        image_uses.record_use(ref, image_id)
    inventory = _inventory(daemon, image_uses.last_uses())
    base, _, tool1, tool2 = inventory.images
    # The base's record names another image id: the tag moved, so it is no use of this image.
    assert base.last_used_at is None
    assert tool1.last_used_at == (NOW - timedelta(days=2)).isoformat()
    assert [row.id for row in inv.candidates(inventory, "unused", 30, NOW)] == ["sha256:base", "sha256:tool2"]
    assert [row.id for row in inv.candidates(inventory, "unused", 1, NOW)] == [
        "sha256:base", "sha256:tool1", "sha256:tool2"]


def test_a_preview_is_used_once_and_expires_after_ten_minutes(fresh_cwd, monkeypatch):
    from collimate_server import docker_inventory as inv
    clock = [1000.0]
    monkeypatch.setattr(inv, "_clock", lambda: clock[0])
    client = _client(_standard())
    first = inv.make_preview(client, "all", None, NOW)
    assert first.wire()["count"] == 3 and first.wire()["expires_at"] == "2026-11-05T12:10:00+00:00"
    assert inv.take_preview(first.id) is first
    assert inv.take_preview(first.id) is None
    second = inv.make_preview(client, "all", None, NOW)
    clock[0] += inv.PREVIEW_TTL_SECONDS
    assert inv.take_preview(second.id) is None
    assert inv.take_preview("not-a-preview") is None


def test_delete_removes_each_collimate_tag_without_force_and_estimates_what_it_freed(fresh_cwd):
    from collimate_server import docker_inventory as inv
    daemon = _standard()
    daemon.images["sha256:tool1"].tags.append("collimate-tool:alias")
    client = _client(daemon)
    preview = inv.make_preview(client, "all", None, NOW)
    result = inv.delete(client, preview, NOW)
    assert sorted(result["removed"]) == ["collimate-linux-small:aaa", "collimate-tool:111",
                                         "collimate-tool:222", "collimate-tool:alias"]
    assert result["failed"] == []
    assert result["bytes_freed_estimate"] == 100 * MB and result["exact"] is True
    assert all(force is False and noprune is False for _, force, noprune in daemon.images_api.removed)
    assert set(daemon.images) == {"sha256:run", "sha256:python"}


def test_delete_rechecks_each_image_and_skips_what_no_longer_qualifies(fresh_cwd, monkeypatch):
    from collimate_server import docker_inventory as inv, image_uses
    monkeypatch.setattr("collimate_server.time_wire.now_iso", lambda: "2026-11-05T12:00:01+00:00")
    daemon = _standard()
    daemon.images["sha256:other"] = _Image("sha256:other", ["collimate-other:1"], [("o1", 4 * MB)])
    daemon.images["sha256:gone"] = _Image("sha256:gone", ["collimate-gone:1"], [("g1", 1 * MB)])
    client = _client(daemon)
    preview = inv.make_preview(client, "all", None, NOW)
    assert preview.wire()["count"] == 5
    daemon.images["sha256:tool1"].tags.append("mine:latest")  # a foreign tag since the preview
    daemon.containers.append("sha256:tool2")  # a container since the preview
    image_uses.record_use("collimate-linux-small:aaa", "sha256:base")  # a use since the preview
    del daemon.images["sha256:gone"]
    with image_uses.ensuring(["collimate-other:1"]):  # a run preparing it now
        result = inv.delete(client, preview, NOW)
    reasons = {row["ref"]: row["reason"] for row in result["failed"]}
    assert result["removed"] == []
    assert reasons == {
        "collimate-tool:111": "it also carries a tag Collimate did not make (mine:latest)",
        "collimate-tool:222": "a container uses it",
        "collimate-linux-small:aaa": "it was used after the preview",
        "collimate-gone:1": "it is no longer on the daemon",
        "collimate-other:1": "a run is preparing it",
    }
    assert result["bytes_freed_estimate"] == 0
    assert daemon.images_api.removed == []


def test_a_docker_refusal_is_reported_with_its_reason(fresh_cwd):
    from collimate_server import docker_inventory as inv
    daemon = _standard()
    daemon.refuse.add("collimate-tool:222")
    client = _client(daemon)
    result = inv.delete(client, inv.make_preview(client, "all", None, NOW), NOW)
    assert result["failed"] == [{"ref": "collimate-tool:222", "reason": "refused collimate-tool:222"}]
    # Only the base and tool1 went: b2 stays under tool2, so 30 MB.
    assert result["bytes_freed_estimate"] == 30 * MB and result["exact"] is True


def test_an_ensure_and_a_removal_of_one_ref_exclude_each_other():
    import threading
    from collimate_server import image_uses
    entered, release = threading.Event(), threading.Event()

    def ensure():
        with image_uses.ensuring(["collimate-x:1"]):
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=ensure)
    with image_uses.removal_claim(["collimate-x:1"]) as claimed:
        assert claimed
        worker.start()
        assert not entered.wait(0.2), "the ensure waits while the removal runs"
    assert entered.wait(5)
    with image_uses.removal_claim(["collimate-x:1"]) as claimed:
        assert not claimed, "no removal while the ensure runs"
    release.set()
    worker.join(5)
    with image_uses.removal_claim(["collimate-x:1"]) as claimed:
        assert claimed


def test_only_collimate_refs_are_recorded(fresh_cwd):
    from collimate_server import image_uses
    image_uses.record_use("python:3.13-slim", "sha256:p")
    image_uses.record_use("reg.local:5000/collimate-x:1", "sha256:r")
    image_uses.record_use("collimate-runner:py3.13-abc", "sha256:c")
    assert set(image_uses.last_uses()) == {"collimate-runner:py3.13-abc"}
    assert image_uses.repository("reg.local:5000/img") == "reg.local:5000/img"
    assert image_uses.repository("collimate-x@sha256:1") == "collimate-x"


def test_the_runner_image_records_its_use_on_cache_hit_and_on_build(fresh_cwd, monkeypatch, tmp_path):
    import asyncio
    from unittest.mock import AsyncMock
    from collimate_server import image_uses, sandbox
    probe = AsyncMock(side_effect=[("missing", ""), ("present", "sha256:built"), ("present", "sha256:built")])
    build = AsyncMock()
    monkeypatch.setattr(sandbox, "_probe_docker_image", probe)
    monkeypatch.setattr(sandbox, "_build_image", build)
    now = ["2026-11-05T10:00:00+00:00"]
    monkeypatch.setattr("collimate_server.time_wire.now_iso", lambda: now[0])
    asyncio.run(sandbox.ensure_image("collimate-runner:py3.13-x", str(tmp_path), lambda _: None))
    assert build.await_count == 1
    assert image_uses.last_uses()["collimate-runner:py3.13-x"] == image_uses.ImageUse(
        "collimate-runner:py3.13-x", "sha256:built", now[0], now[0])
    now[0] = "2026-11-05T11:00:00+00:00"
    asyncio.run(sandbox.ensure_image("collimate-runner:py3.13-x", str(tmp_path), lambda _: None))
    assert build.await_count == 1
    use = image_uses.last_uses()["collimate-runner:py3.13-x"]
    assert (use.first_used_at, use.last_used_at) == ("2026-11-05T10:00:00+00:00", now[0])
    assert not image_uses._ensuring


# -- the routes ---------------------------------------------------------------

@pytest.fixture
def storage_client(api_app):
    from fastapi.testclient import TestClient
    with TestClient(api_app) as client:
        yield client


def test_the_routes_preview_then_delete_and_refuse_an_expired_preview(storage_client, monkeypatch):
    from collimate_server import docker_inventory as inv
    daemon = _standard()
    monkeypatch.setattr(inv, "docker_client", lambda: _client(daemon))
    listing = storage_client.get("/api/storage/docker").json()
    assert listing["available"] is True and len(listing["images"]) == 4
    assert "last_used_at" not in listing["images"][0]

    assert storage_client.post("/api/storage/docker/preview", json={"mode": "unused"}).status_code == 400
    assert storage_client.post("/api/storage/docker/preview", json={"mode": "some"}).status_code == 422
    preview = storage_client.post("/api/storage/docker/preview", json={"mode": "unused", "days": 30}).json()
    assert (preview["count"], preview["bytes"], preview["exact"]) == (3, 100 * MB, True)
    assert [row["id"] for row in preview["images"]] == ["sha256:base", "sha256:tool1", "sha256:tool2"]
    done = storage_client.post("/api/storage/docker/delete", json={"preview_id": preview["preview_id"]})
    assert done.status_code == 200, done.text
    assert done.json()["bytes_freed_estimate"] == 100 * MB and len(done.json()["removed"]) == 3
    again = storage_client.post("/api/storage/docker/delete", json={"preview_id": preview["preview_id"]})
    assert again.status_code == 404
    assert daemon.closed


def test_the_routes_say_when_docker_is_unreachable(storage_client, monkeypatch):
    from collimate_server import docker_inventory as inv

    def unreachable():
        raise inv.DockerUnavailable("Cannot connect to the Docker daemon")
    monkeypatch.setattr(inv, "docker_client", unreachable)
    assert storage_client.get("/api/storage/docker").json() == {
        "available": False, "reason": "Cannot connect to the Docker daemon"}
    response = storage_client.post("/api/storage/docker/preview", json={"mode": "all"})
    assert response.status_code == 503 and "Cannot connect" in response.json()["detail"]
    assert storage_client.post("/api/storage/docker/delete", json={"preview_id": "x"}).status_code == 503


# -- live ---------------------------------------------------------------------

def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                              capture_output=True, timeout=15).returncode == 0
    except Exception:
        return False


requires_docker = pytest.mark.skipif(not _docker_available(), reason="docker daemon not reachable")


@requires_docker
def test_live_two_layered_tags_and_the_unused_one_is_deleted(fresh_cwd, monkeypatch):
    """Builds a base and two images on it through the environment service,
    records a use of one, previews ``unused`` and deletes: only the unused
    image's tag goes, the other stays, and the reclaim is positive."""
    import docker
    from collimate_server import docker_inventory as inv, image_uses
    from collimate_server.database import get_global_db_connection
    from collimate_server.process_manager.environments import EnvironmentService
    from collimate_server.runner_permissions import RunnerPermissions

    stamp = uuid.uuid4().hex[:10]
    base, kept, dropped = (f"collimate-livetest-{n}-{stamp}:1" for n in ("base", "kept", "dropped"))
    contexts = {}
    # debian:trixie-slim is the base image's own FROM, so a machine that ran
    # Collimate holds it: the builds need no network.
    for name, dockerfile in ((base, f"FROM debian:trixie-slim\nRUN echo {stamp} > /base\n"),
                             (kept, f"FROM {base}\nRUN echo kept > /kept\n"),
                             (dropped, f"FROM {base}\nRUN head -c 300000 /dev/urandom > /dropped\n")):
        context = fresh_cwd / name.split(":")[0]
        context.mkdir()
        (context / "Dockerfile").write_text(dockerfile)
        contexts[name] = str(context)

    class Manager:
        run_id = "run-live-docker-cleanup"
        permissions = RunnerPermissions.from_definition({"docker": "full"})
        log_buffer: list = []

    client = docker.from_env()
    service = EnvironmentService(Manager(), client_factory=lambda: client)
    try:
        for name in (kept, dropped):
            service._ensure_image(client, name, contexts[name], base, contexts[base], refresh=False)
        assert client.images.get(kept).labels.get("com.collimate.built") == "1"
        # Only kept's use stays recent: the others were used 60 days ago.
        old = (datetime.now(timezone.utc) - timedelta(days=60)).replace(microsecond=0).isoformat()
        conn = get_global_db_connection()
        with conn:
            conn.execute("UPDATE image_uses SET last_used_at=? WHERE ref<>?", (old, kept))
        conn.close()
        assert set(image_uses.last_uses()) == {base, kept, dropped}

        preview = inv.make_preview(client, "unused", 30, datetime.now(timezone.utc))
        ours = [row for row in preview.images if stamp in row.refs[0]]
        assert sorted(row.refs[0] for row in ours) == sorted([base, dropped])
        # Delete only our own images, whatever else the daemon holds unused.
        mine = inv.Preview(**{**preview.__dict__, "images": tuple(row for row in ours if row.refs == (dropped,))})
        result = inv.delete(client, mine, datetime.now(timezone.utc))
        assert result["removed"] == [dropped] and result["failed"] == []
        # The estimate comes from the preview's size figures, which a daemon
        # may report as 0 for a just-built image (GitHub's runner does): the
        # estimate is then 0 too, consistently, and the deletion itself is
        # what this test proves.
        reported = next(row for row in ours if row.refs == (dropped,)).unique_size
        assert result["bytes_freed_estimate"] >= min(reported, 300_000)
        with pytest.raises(docker.errors.ImageNotFound):
            client.images.get(dropped)
        client.images.get(kept)
    finally:
        for name in (kept, dropped, base):
            try:
                client.images.remove(name, force=True)
            except docker.errors.ImageNotFound:
                pass
        client.close()
