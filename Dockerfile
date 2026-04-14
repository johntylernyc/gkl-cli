FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml uv.lock ./
COPY gkl/ gkl/

# Install dependencies including web extras
RUN uv sync --extra web --no-dev --frozen

# Create data directory for volumes
RUN mkdir -p /data

EXPOSE 8080

CMD ["uv", "run", "gkl-web"]
