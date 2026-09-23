import re
from collections.abc import Sequence
from pathlib import Path

import pathspec

READ_MAX_LINES = 400
GREP_MAX_MATCHES = 200
LINE_MAX_CHARS = 300


class ToolError(Exception):
    pass


def _is_binary(path: Path) -> bool:
    with path.open("rb") as f:
        return b"\0" in f.read(8192)


class Workspace:
    def __init__(self, root: Path, exclude: Sequence[str] = ()) -> None:
        self.root = root.resolve()
        patterns = [".git/", *exclude]
        gitignore = self.root / ".gitignore"
        if gitignore.is_file():
            patterns += gitignore.read_text(errors="replace").splitlines()
        self._ignore = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
        self.files_read: set[str] = set()
        self.truncations: list[str] = []
        self._files: list[str] | None = None

    def _resolve(self, rel: str) -> Path:
        if Path(rel).is_absolute():
            raise ToolError(f"{rel}: outside the repository")
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root):
            raise ToolError(f"{rel}: outside the repository")
        rel_posix = path.relative_to(self.root).as_posix()
        if rel_posix != "." and self._ignore.match_file(rel_posix + ("/" if path.is_dir() else "")):
            raise ToolError(f"{rel}: ignored path")
        return path

    def files(self) -> list[str]:
        if self._files is None:
            found = []
            for path in self.root.rglob("*"):
                rel = path.relative_to(self.root).as_posix()
                if path.is_symlink() or not path.is_file() or self._ignore.match_file(rel):
                    continue
                if not _is_binary(path):
                    found.append(rel)
            self._files = sorted(found)
        return self._files

    def list_dir(self, path: str = ".") -> str:
        target = self._resolve(path)
        if not target.is_dir():
            raise ToolError(f"{path}: not a directory")
        prefix = "" if target == self.root else target.relative_to(self.root).as_posix() + "/"
        entries = sorted(
            {
                f[len(prefix) :].split("/", 1)[0] + ("/" if "/" in f[len(prefix) :] else "")
                for f in self.files()
                if f.startswith(prefix)
            }
        )
        return "\n".join(entries) or "(empty)"

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"{path}: not a file")
        if _is_binary(target):
            raise ToolError(f"{path}: binary file")
        lines = target.read_text(errors="replace").splitlines()
        start = max(start, 1)
        last = min(end or len(lines), len(lines), start + READ_MAX_LINES - 1)
        rel = target.relative_to(self.root).as_posix()
        self.files_read.add(rel)
        out = [f"{n}: {lines[n - 1][:LINE_MAX_CHARS]}" for n in range(start, last + 1)]
        wanted_end = min(end or len(lines), len(lines))
        if last < wanted_end:
            note = f"read {rel}: lines {start}-{last} of {start}-{wanted_end} shown"
            self.truncations.append(note)
            out.append(f"[truncated: {note}; ask for the next range]")
        return "\n".join(out)

    def grep(self, pattern: str, path_glob: str = "**/*") -> str:
        try:
            regex = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"invalid pattern: {e}") from e
        glob = pathspec.PathSpec.from_lines("gitwildmatch", [path_glob])
        hits = []
        for rel in self.files():
            if not glob.match_file(rel):
                continue
            text = (self.root / rel).read_text(errors="replace")
            for n, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{rel}:{n}: {line[:LINE_MAX_CHARS]}")
        if len(hits) > GREP_MAX_MATCHES:
            self.truncations.append(
                f"grep '{pattern}': {GREP_MAX_MATCHES} of {len(hits)} matches shown"
            )
            return "\n".join(hits[:GREP_MAX_MATCHES]) + (
                f"\n[truncated: {GREP_MAX_MATCHES} of {len(hits)} matches shown]"
            )
        return "\n".join(hits) or "(no matches)"
