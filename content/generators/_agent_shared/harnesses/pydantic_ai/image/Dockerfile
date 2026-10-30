# The pydantic-ai harness's command sandbox: the container Coder's shell (and,
# while it runs, its file tools) works in under docker: full with commands ask
# or allow. The agent itself is a process on this machine (Pydantic AI's ACP
# adapter, harnesses/pydantic_ai/server.py); only its workspace is here.
#
# The host places it like every harness container — the run workspace
# bind-mounted rw at /workspace, `coder` (uid 1000) the user — and reaps it at
# run end. The label is Pydantic AI Harness's own (DockerSandbox attaches only
# to a container carrying it), set on the image so the placed container has it.
ARG BASE=collimate-linux-small:v3
FROM ${BASE}

LABEL ai.pydantic.workspace=true

# /run/pydantic-ai holds the pid files DockerSandbox stops a timed-out
# command by (its own containers get a tmpfs there); the user must write it.
RUN useradd -m -s /bin/bash -u 1000 coder \
    && mkdir -p /workspace /run/pydantic-ai \
    && chown coder:coder /workspace /run/pydantic-ai \
    && chmod 700 /run/pydantic-ai

USER coder
WORKDIR /workspace

CMD ["sleep", "infinity"]
