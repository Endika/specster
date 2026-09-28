FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PYTHONSAFEPATH=1 \
    TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=/app/.cache/tree-sitter-language-pack
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*
# mise installs the toolchains a repository declares (node, java, ruby, ...), so projects in other
# or mixed languages can build; pinned, and checked against the release's published sha256.
ARG MISE_VERSION=v2026.9.12
ARG MISE_SHA256=b4058dece685259910d3aba5782445996eea79dbdb3cf952a6eb81aadf0373ff
RUN python -c "import hashlib, sys, urllib.request; v, want = sys.argv[1:]; \
data = urllib.request.urlopen(f'https://github.com/jdx/mise/releases/download/{v}/mise-{v}-linux-x64.tar.gz').read(); \
got = hashlib.sha256(data).hexdigest(); \
sys.exit(f'mise {v}: sha256 {got}, expected {want}') if got != want else open('/tmp/mise.tgz', 'wb').write(data)" \
        "$MISE_VERSION" "$MISE_SHA256" \
    && tar xzf /tmp/mise.tgz -C /opt \
    && ln -s /opt/mise/bin/mise /usr/local/bin/mise \
    && rm /tmp/mise.tgz \
    && mise --version
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
RUN /app/.venv/bin/python -c "from specster.repomap import EXT_LANG; from tree_sitter_language_pack import get_parser; [get_parser(lang) for lang in set(EXT_LANG.values())]" \
    && chmod -R a+rX /app/.cache
# GitHub runs Docker actions as root and mounts the workspace owned by the runner; a USER line breaks file access.
# GitHub runs the action with workdir /github/workspace: -P keeps the analyzed repo's
# secrets.py or yaml.py from shadowing our imports in the process that holds the tokens.
# GitHub starts Docker actions without --init: tini as PID 1 reaps the orphans a test kills,
# which would otherwise stay zombies that still answer kill(pid, 0) until the run ends.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/.venv/bin/python", "-P", "-m", "specster"]
