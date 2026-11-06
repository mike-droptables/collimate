# Batteries-included terminal image for the `terminal` / `lazygit` runners
# (built lazily by the launch generator from this `build` config folder). Layers a
# git workflow onto the shared collimate-linux-small base: a non-root `coder`
# user (uid 1000, so bind-mounted workspace writes stay host-owned) with bash +
# git + gh + lazygit, plus openssh-client (ssh remotes), less (pager), and sudo
# (the optional sudo setting). PID 1 is `sleep infinity` — the run execs the
# configured terminal app into the container.
#
# The `terminal` and `lazygit` runners ship an identical copy of this file, so
# the content hash (and therefore the built image tag) is shared — it builds
# once and both runners reuse it.
# The builder injects the resolved content-addressed base tag; the literal
# default keeps a hand-run `docker build` working.
ARG BASE=collimate-linux-small:v3
FROM ${BASE}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openssh-client \
        less \
        sudo \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# lazygit ships as a static release binary, independently of the distro package.
#
# The ARG default is the offline fallback: launch's run.py _requested_versions
# builds the latest release (folded into the image tag) unless the runner pins
# a version in `packages:`.
ARG LAZYGIT_VERSION=0.66.2
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) lg_arch="x86_64" ;; \
        arm64) lg_arch="arm64" ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/lazygit.tar.gz \
        "https://github.com/jesseduffield/lazygit/releases/download/v${LAZYGIT_VERSION}/lazygit_${LAZYGIT_VERSION}_linux_${lg_arch}.tar.gz"; \
    tar --no-same-owner -C /usr/local/bin -xzf /tmp/lazygit.tar.gz lazygit; \
    rm /tmp/lazygit.tar.gz; \
    lazygit --version

RUN useradd -m -s /bin/bash -u 1000 coder

# Pin HOME so `docker exec` (which doesn't always populate it) finds the
# coder user's ~/.gitconfig and ~/.git-credentials.
ENV HOME=/home/coder

USER coder
WORKDIR /home/coder

CMD ["sleep", "infinity"]
