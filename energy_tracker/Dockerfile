# Used both by Home Assistant (as an add-on) and by Docker Compose (for development).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so code changes don't re-download packages.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src ./src
COPY config ./config

# Runs as the container's default user: Home Assistant owns the /data folder the database
# lives in. The container has no published ports and no access to the host.
CMD ["python", "-m", "energy_tracker"]
