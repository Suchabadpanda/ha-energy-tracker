# Used both by Home Assistant (as an add-on) and by Docker Compose (for development).
# Built in two stages: the first installs the Python packages with uv, the second copies
# only the result, so the finished image carries neither uv nor its caches.

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY src ./src
COPY config ./config

LABEL io.hass.type="addon" \
      org.opencontainers.image.source="https://github.com/Suchabadpanda/ha-energy-tracker"

# Runs as the container's default user: Home Assistant owns the /data folder the database
# lives in. The container has no published ports and no access to the host.
CMD ["python", "-m", "energy_tracker"]
