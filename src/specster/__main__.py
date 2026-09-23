import sys

from specster import __version__


def cli(argv: list[str]) -> int:
    if argv[:1] == ["--version"]:
        print(f"specster {__version__}")
        return 0
    print("usage: python -m specster [--version]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
