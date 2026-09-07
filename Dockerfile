FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Dependencies in their own layer, cached unless pyproject.toml/uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . .
RUN uv sync --frozen

# var/ (token + job queue) and data/ (the export) are meant to be bind-mounted
# from the host - see docker-compose.yml - so they land on your machine, not
# just inside the container, and survive a rebuild/`docker compose down`.
#
# The container runs as a raw numeric UID with no /etc/passwd entry (no
# "docker exec"-created user), so $HOME can't be resolved - that's the
# "I have no name!" prompt - and `uv run` falls back to an unwritable
# `/.cache/uv`. Setting HOME and UV_CACHE_DIR fixes that; `uv run` also
# re-checks the venv is in sync on every invocation and rewrites its console
# scripts (currently root-owned, from the `uv sync` steps above) to do it -
# so the whole app dir needs to belong to the runtime user, not just var/data.
ENV HOME=/app
ENV UV_CACHE_DIR=/app/.uv-cache
RUN mkdir -p /app/var /app/data /app/.uv-cache && chown -R 1000:1000 /app
USER 1000:1000

# So plain `serve` / `import-all` work too, without the /app/.venv/bin/
# prefix or `uv run` at all.
ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 5050

# `serve` auto-starts, so `docker compose up` alone gets you a working app at
# http://localhost:5050 - no manual step, no risk of the container showing
# "Up" while nothing's actually listening. `docker compose exec asana-migration
# bash` still works anytime (e.g. for `import-all`); it runs safely alongside
# this, sharing the same mounted data/var.
CMD ["serve", "--host", "0.0.0.0", "--no-browser"]
