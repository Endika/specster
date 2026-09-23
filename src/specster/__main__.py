import os
import sys

from specster import __version__
from specster.github import GitHubRest
from specster.llm.factory import build_chat_model
from specster.run import env_from, main
from specster.skills import http_fetch


def cli(argv: list[str]) -> int:
    if argv[:1] == ["--version"]:
        print(f"specster {__version__}")
        return 0
    if argv[:1] == ["--self-check"]:
        from pathlib import Path

        from specster.repomap import symbols_for

        source = Path(__file__).with_name("run.py").read_bytes()
        found = symbols_for("run.py", source)
        print(f"specster {__version__}: {len(found)} symbols in run.py")
        return 0 if any(s.startswith("def main(") for s in found) else 1
    env = env_from(os.environ)
    tracker = GitHubRest(env.repo, env.token, env.api_url, env.graphql_url)
    return main(env, tracker, lambda cfg: build_chat_model(cfg, env.secrets), http_fetch)


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
