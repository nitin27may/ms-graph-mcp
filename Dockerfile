# syntax=docker/dockerfile:1
#
# Only the HTTP transport makes sense in a container. stdio speaks JSON-RPC over
# the process's own stdin/stdout, which a client has to spawn directly — putting
# that behind `docker run` gains nothing and breaks the interactive sign-in.
#
#   docker build -t ms-graph-mcp .
#   docker run --rm -p 8094:8094 \
#     -e GRAPH_MCP_CLIENT_ID=… -e GRAPH_MCP_TENANT_ID=… \
#     -e GRAPH_MCP_RESOURCE_URL=https://graph-mcp.example.com/mcp \
#     ms-graph-mcp

ARG PYTHON_VERSION=3.13

# ─── build ────────────────────────────────────────────────────────────────────
# Builds a wheel and installs it into a self-contained virtualenv, which is then
# copied wholesale into the runtime stage. uv, the lockfile and the build
# toolchain all stay behind in this stage.
FROM python:${PYTHON_VERSION}-slim AS build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# The venv is built at its *final* path, not at /src/.venv. Console-script
# shebangs are absolute, so a venv created elsewhere and copied in points at a
# python that does not exist in the runtime stage — the container then dies with
# a bare "no such file or directory" naming the script rather than the shebang.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /src

# Dependencies resolve from the lockfile before the source is copied, so editing
# a tool does not invalidate the dependency layer. --no-install-project is what
# keeps the project itself out of this step.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src/ ./src/
COPY README.md LICENSE ./

# --no-editable matters: the default installs the project as a .pth pointing at
# /src/src, which does not exist in the runtime stage. The container would then
# start and immediately die on `ModuleNotFoundError: ms_graph_mcp`.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ─── runtime ──────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

# curl is here for HEALTHCHECK and nothing else. Removing it means the
# healthcheck below has to be rewritten — it is not incidental.
#
# Deliberately unpinned: Debian drops old point versions from the repo as it
# rebuilds, so a pinned curl turns into a build that fails weeks later for a
# reason unrelated to any change here.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root, and owning nothing it runs. A compromised process cannot rewrite its
# own code because the venv belongs to root and is only readable here.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app

COPY --from=build --chown=root:root /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GRAPH_MCP_PORT=8094

# Numeric, not `app`. Kubernetes `runAsNonRoot` cannot resolve a *named* user
# from outside the image, so it refuses to admit the pod rather than assume.
USER 10001
WORKDIR /home/app
EXPOSE 8094

# /health is deliberately unauthenticated so this needs no credentials. It does
# not touch Graph, so a healthy container here means the process is serving —
# not that Entra or Graph are reachable.
#
# Shell form is required here, not a style choice: `||` and the ${…} expansion
# both need a shell, and JSON form would exec curl directly with the variable
# unexpanded.
# hadolint ignore=DL3025
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${GRAPH_MCP_PORT}/health" || exit 1

# Exec form, so the process is PID 1 and receives SIGTERM directly rather than
# through a shell that would swallow it and force a 10-second kill on every
# deploy.
CMD ["ms-graph-mcp-http"]
