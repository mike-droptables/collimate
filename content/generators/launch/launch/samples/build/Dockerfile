# Sample `build` config folder — a starter Dockerfile a runner can hand the run
# generator. Docs-only: this sample is never staged into a run (it just shows
# the shape). A runner ships its real Dockerfile + context under
# <runner-package>/files/build/.
#
# Take the shared base (Debian + python + node) as a BUILD ARG, never as a
# literal tag. The base is built under a CONTENT-ADDRESSED tag — its identity is
# a hash of _env_shared/base, so an edit there reaches every image layered on it
# — and the run generator injects that resolved tag as `BASE`. A literal
# `FROM collimate-linux-small:v3` names a tag that is never built and the build
# fails ("pull access denied").
#
# The default below is load-bearing twice over: it keeps a hand-run
# `docker build` working, AND the run generator decides whether to build the
# shared base at all by looking for the base's name in this file. Drop the
# default and no base is ensured, so the build has nothing to layer on.
ARG BASE=collimate-linux-small:v3
FROM ${BASE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/*

# An app this image installs by NAME should take its version as an ARG with a
# PINNED default, never a floating install: `myapp@latest` makes the image tag
# LIE, since the content hash holds still while what you installed moves.
#   ARG MYAPP_VERSION=1.2.3
#   RUN npm install -g myapp@${MYAPP_VERSION}
# Only the tools the run generator knows (lazygit, claude-code, uv, via
# LAZYGIT_VERSION / CLAUDE_CODE_VERSION / UV_VERSION) get more: declare that
# ARG and the generator resolves the version (latest unless the runner's
# `packages` block pins it, e.g. `packages: {claude-code: 2.0.0}`), injects it
# here and folds it into the image tag, so a new version rebuilds and an
# unchanged one is a cache hit. Any other app keeps its ARG default: bump it by
# editing this file.

# PID 1 just needs to stay alive — the run execs into the container.
CMD ["sleep", "infinity"]
