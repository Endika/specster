FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PYTHONSAFEPATH=1 \
    TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=/app/.cache/tree-sitter-language-pack
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# One uid/gid per sandbox slot (0 = final integration tests, 1..8 = workers), so getpwuid works.
RUN for i in 0 1 2 3 4 5 6 7 8; do \
        groupadd --gid "$((61000 + i))" "specster-sb$i" \
        && useradd --no-log-init --no-create-home --uid "$((61000 + i))" --gid "$((61000 + i))" \
            --home-dir /nonexistent --shell /usr/sbin/nologin "specster-sb$i" \
        || exit 1; \
    done
# No setuid/setgid binary a test process could use to leave its sandbox uid.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} +
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev
# tree-sitter-language-pack downloads grammars over the network on first use;
# bake every language in EXT_LANG into the image so --self-check works with --network none.
RUN /app/.venv/bin/python -c "from specster.repomap import EXT_LANG; from tree_sitter_language_pack import get_parser; [get_parser(lang) for lang in set(EXT_LANG.values())]"
# GitHub runs Docker actions as root and mounts the workspace owned by the runner; a USER line breaks file access.
# GitHub runs the action with workdir /github/workspace: -P keeps the analyzed repo's
# secrets.py or yaml.py from shadowing our imports in the process that holds the tokens.
ENTRYPOINT ["/app/.venv/bin/python", "-P", "-m", "specster"]
