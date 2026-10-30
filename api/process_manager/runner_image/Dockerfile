# Collimate default runner image — hosts a generator's MAIN LOOP for
# ``docker: limited`` runs (runners whose grant is docker: limited; see
# api/process_manager/docker_runner.py).
#
# Two hard properties, matching the pinned docker-ladder semantics:
#   * NO docker CLI and NO docker socket in the image — ``limited`` grants
#     "run the main loop in a container", NOT "spawn further containers".
#     The absence of the CLI/socket is what makes that a hard guarantee.
#   * The launch is the SAME ``uv run <entrypoint>`` the native path uses,
#     so a generator behaves identically whether it runs native or boxed.
#
# The Python here MUST match the host's major.minor (docker_runner derives
# the tag from the build context and host interpreter) so the SDK
# bind-mounted read-only from the host resolves the same way under ``uv``.
ARG PYTHON_VERSION
FROM python:${PYTHON_VERSION}-slim

# uv, pinned + copied from the official image (offline after the first
# pull; no pip-install round-trip). Change this pin to refresh image identity.
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /usr/local/bin/

# uv must never reach the network to fetch a managed interpreter — an
# egress-bounded run (internet: none/proxy) would hang. Force the system
# interpreter (the host-matching version, above), and point uv's cache + HOME at world-writable
# tmp so a ``--user <host-uid>`` container (docker_runner runs as the host
# uid so workspace writes stay host-owned) has somewhere to write.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-system \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_LINK_MODE=copy \
    HOME=/tmp \
    PYTHONUNBUFFERED=1

# A generator's own PEP 723 deps still resolve at ``uv run`` time. Under an
# egress-bounded tag that fetch is blocked by design, so ``docker: limited``
# tags are expected to be dependency-light (the default runner carries only
# the stdlib + the host-mounted SDK on PYTHONPATH) or to ship a warmed
# UV_CACHE_DIR via a derived image.
