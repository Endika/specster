FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PYTHONSAFEPATH=1 \
    TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=/app/.cache/tree-sitter-language-pack
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
