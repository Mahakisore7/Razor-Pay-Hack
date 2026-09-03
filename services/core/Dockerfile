# syntax=docker/dockerfile:1
#
# Builds the one image used by api, worker, and scheduler (ARCHITECTURE §9) --
# they differ only by the command docker-compose.yml gives them. Multi-stage
# so the runtime image carries a venv and source, never uv, a lockfile, or a
# build cache (SECURITY §4: minimal image, non-root).

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, pinned by the lockfile, in their own layer -- a source
# change should not bust the dependency-install cache.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime

RUN groupadd --system recoup && \
    useradd --system --gid recoup --no-create-home --shell /usr/sbin/nologin recoup

WORKDIR /app

COPY --from=builder --chown=recoup:recoup /app/.venv /app/.venv
COPY --from=builder --chown=recoup:recoup /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER recoup

EXPOSE 8000

# Only the api service actually answers this over HTTP; worker and scheduler
# override HEALTHCHECK in docker-compose.yml with their heartbeat-file check
# (CLI.md: `recoup worker`/`recoup scheduler` are Phase 0 placeholders with no
# HTTP surface of their own). Using the stdlib rather than curl/wget keeps the
# runtime image free of a package installed for nothing but this check.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import sys,urllib.request as u; sys.exit(0 if u.urlopen('http://localhost:8000/health/live', timeout=2).status == 200 else 1)"]

ENTRYPOINT ["recoup"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
