FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Dependencies in their own layer, cached unless pyproject.toml/uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . .
RUN uv sync --frozen

ENV HOME=/app
ENV UV_CACHE_DIR=/app/.uv-cache
RUN mkdir -p /app/var /app/data /app/.uv-cache && chown -R 1000:1000 /app
USER 1000:1000

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 5050

# if dont need to directly run the serve command
# CMD ["sleep", "infinity"]
CMD ["serve", "--no-browser"]
