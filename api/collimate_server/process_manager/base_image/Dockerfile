# collimate-linux-small — the host's Debian base image (canonical, single copy).
#
# One lean base for any generator's container: the launch generator's Docker modes,
# the agent harness images and any in-app generator's image, so they share layers instead of each
# carrying their own python + node install. Enough toolchain that layered images
# add their tooling in a single apt/npm step and workers can run typical
# build/test loops.
#
# Node comes from NodeSource at 24.x (the active LTS) so harnesses share one supported major.
# The distro package can lag what opencode and the Claude CLI need. Kept lean: --no-install-recommends
# and cleaned apt lists so downstream images stay small. The setup script is
# downloaded before it runs, not piped into bash: RUN has no pipefail, so a failed
# download would otherwise fall through to a distro Node without npm and be
# cached as the base. The version check fails the layer if that ever happens.
#
# This is the ONE source of truth for the base, owned by the host beside the
# runner image (api/collimate_server/process_manager/base_image.py stages it). It is built under a
# content-addressed tag, collimate-linux-small:<12 hex of this folder>
# (collimate.image_tags.derive_image_tag of the staged folder: this file and the
# host's SDK), so any edit here, comments included, re-tags
# it and every image layered on it. Layered images take it as
# `ARG BASE=collimate-linux-small:v3` / `FROM ${BASE}`: the host injects the
# resolved tag, and the literal default only serves a hand-run build (tag the
# base by hand, or pass --build-arg BASE=...).
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git procps gnupg \
        python3 python3-pip python3-venv python-is-python3 \
    && curl -fsSL -o /tmp/nodesource_setup.sh https://deb.nodesource.com/setup_24.x \
    && bash /tmp/nodesource_setup.sh && rm -f /tmp/nodesource_setup.sh \
    && apt-get install -y --no-install-recommends nodejs \
    && node --version | grep -q '^v24\.' && npm --version \
    && rm -rf /var/lib/apt/lists/*

# The host stages the SDK subset of the current collimate distribution
# beside this file: its Python packages collimate and gen, and the
# distribution's metadata (never the server, collimate_server). /opt/collimate
# holds them alone: a draft test's `uv run` child takes that directory as its
# PYTHONPATH.
COPY sdk /opt/collimate
# Staging is private on the host. This public image copy must also be readable
# by the host UID used for commands, while remaining writable only by root.
RUN chmod -R u=rwX,go=rX /opt/collimate
# The SDK packages' runtime dependencies (pyyaml): collimate.shared.sdk_requirements.
COPY sdk-requirements.txt /opt/sdk-requirements.txt
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
# The image's own Python is this venv, first on PATH, with pip seeded so launch
# setup scripts use it rather than the externally managed system interpreter.
# The SDK is linked into its site-packages, so `import collimate` and `import
# gen` work in a container shell and in launch's per-user venv layered on it.
# uv runs outside a project (`uv run --no-project --with pytest ...`) on the
# managed interpreter instead, never on a venv found on PATH: a .pth there adds
# this venv's site-packages after the managed one's. No PYTHONPATH: it would
# sort ahead of every project venv's site-packages and shadow a project's own
# collimate or gen, while a project venv never reads either site-packages.
ARG UV_VERSION=0.13.5
RUN pip3 install --break-system-packages --no-cache-dir uv==${UV_VERSION} \
    && uv python install 3.13 \
    && uv venv --seed --python 3.13 /opt/collimate-python \
    && uv pip install --python /opt/collimate-python/bin/python -r /opt/sdk-requirements.txt \
    && purelib='import sysconfig; print(sysconfig.get_path("purelib"))' \
    && site="$(/opt/collimate-python/bin/python -c "$purelib")" \
    && ln -s /opt/collimate/collimate /opt/collimate/gen /opt/collimate/collimate-*.dist-info "$site/" \
    && echo "$site" > "$("$(uv python find --managed-python 3.13)" -c "$purelib")/collimate-python.pth" \
    && printf '%s\n' 'case ":$PATH:" in *:/opt/collimate-python/bin:*) ;; *) PATH="/opt/collimate-python/bin:$PATH" ;; esac' \
        > /etc/profile.d/collimate-python.sh
# Login shells (`bash -lc`) reset PATH in /etc/profile; the profile.d line above
# puts this venv back in front there.
ENV PATH=/opt/collimate-python/bin:$PATH

# A non-login default; generators bind-mount the cell workspace at /work.
WORKDIR /work

# PID 1 just needs to stay alive — the environment execs into it.
CMD ["sleep", "infinity"]
