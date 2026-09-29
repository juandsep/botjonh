FROM python:3.14-slim AS builder

# Pinned uv release, not `latest`: the build must not change under our feet.
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project
COPY src ./src
RUN uv sync --locked

FROM python:3.14-slim

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/src ./src
COPY --from=builder /app/pyproject.toml ./

# Non-root: the serving process must not be able to write outside its own home.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

ENV HOME=/home/app PATH="/opt/venv/bin:$PATH" PORT=8080
EXPOSE 8080

# Default entrypoint is the webhook; the worker overrides it with
# `uvicorn assistant.worker:app` (see .github/workflows/deploy.yml).
CMD ["sh", "-c", "uvicorn assistant.api:app --host 0.0.0.0 --port ${PORT}"]
