# collimate-linux-small — the shared Debian base image (canonical, single copy).
#
# One lean base for the launch generator's Docker modes AND the agent harness
# images (_agent_shared/*/Dockerfile), so they share layers instead of each
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
# This is the ONE source of truth for the base. It is built under a
# content-addressed tag, collimate-linux-small:<12 hex of this folder>
# (_env_shared.derive_image_tag), so any edit here, comments included, re-tags
# it and every image layered on it. Layered images take it as
# `ARG BASE=collimate-linux-small:v3` / `FROM ${BASE}`: the builder injects the
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

# Draft-generator tests run with their declared uv dependencies and the same SDK
# as their parent. base_context stages canonical SDK bytes beside this file.
COPY sdk /opt/collimate-sdk
# Staging is private on the host. This public image copy must also be readable
# by the host UID used for commands, while remaining writable only by root.
RUN chmod -R u=rwX,go=rX /opt/collimate-sdk
COPY sdk-requirements.txt /opt/sdk-requirements.txt
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
ENV PYTHONPATH=/opt/collimate-sdk
# Seed pip inside the managed venv so launch setup scripts use this Python,
# rather than the externally managed system interpreter.
ARG UV_VERSION=0.13.5
RUN pip3 install --break-system-packages --no-cache-dir uv==${UV_VERSION} \
    && uv python install 3.13 \
    && uv venv --seed --python 3.13 /opt/collimate-python \
    && uv pip install --python /opt/collimate-python/bin/python -r /opt/sdk-requirements.txt
ENV PATH=/opt/collimate-python/bin:$PATH

# A non-login default; generators bind-mount the cell workspace at /work.
WORKDIR /work

# PID 1 just needs to stay alive — the environment execs into it.
CMD ["sleep", "infinity"]
