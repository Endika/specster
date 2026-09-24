import contextlib
import os
import re
import stat
from collections.abc import Sequence
from pathlib import Path

import pathspec

from specster.plan import norm_path

READ_MAX_LINES = 400
GREP_MAX_MATCHES = 200
LIST_MAX_ENTRIES = 500
LINE_MAX_CHARS = 300
FILE_MAX_BYTES = 2_000_000
# google-github-actions/auth writes gha-creds-*.json into the workspace. Kept apart from the
# repo's .gitignore so a "!" line there cannot bring these back.
HARD_EXCLUDE = (".git/", "gha-creds-*.json")


class ToolError(Exception):
    pass


def has_git_component(path: str) -> bool:
    """A `.git` file is a gitdir pointer: root's git would silently commit nothing under it."""
    return any(part.lower() == ".git" for part in path.split("/"))


WORKFLOW_DIRS = (".github/workflows", ".github/actions")


def is_workflow_path(path: str) -> bool:
    """Workflow and action code runs in CI with the repository's secrets once pushed."""
    rel = norm_path(path).lower()
    return any(rel == d or rel.startswith(f"{d}/") for d in WORKFLOW_DIRS)


def _is_binary(path: Path) -> bool:
    with path.open("rb") as f:
        return b"\0" in f.read(8192)


class Workspace:
    def __init__(self, root: Path, exclude: Sequence[str] = ()) -> None:
        self.root = root.resolve()
        patterns = list(exclude)
        gitignore = self.root / ".gitignore"
        if gitignore.is_file():
            patterns += gitignore.read_text(errors="replace").splitlines()
        self._repo_ignore = pathspec.GitIgnoreSpec.from_lines(patterns)
        self._hard_ignore = pathspec.GitIgnoreSpec.from_lines(HARD_EXCLUDE)
        self.files_read: set[str] = set()
        self.truncations: list[str] = []
        self._files: list[str] | None = None

    def _ignored(self, rel_posix: str) -> bool:
        return self._hard_ignore.match_file(rel_posix) or self._repo_ignore.match_file(rel_posix)

    def _resolve(self, rel: str) -> Path:
        if Path(rel).is_absolute():
            raise ToolError(f"{rel}: outside the repository")
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root):
            raise ToolError(f"{rel}: outside the repository")
        rel_posix = path.relative_to(self.root).as_posix()
        if rel_posix != "." and self._ignored(rel_posix + ("/" if path.is_dir() else "")):
            raise ToolError(f"{rel}: ignored path")
        return path

    def files(self) -> list[str]:
        if self._files is None:
            found = []
            skipped_large = []
            for path in self.root.rglob("*"):
                rel = path.relative_to(self.root).as_posix()
                if path.is_symlink() or not path.is_file() or self._ignored(rel):
                    continue
                if path.stat().st_size > FILE_MAX_BYTES:
                    skipped_large.append(rel)
                    continue
                if not _is_binary(path):
                    found.append(rel)
            self._files = sorted(found)
            if skipped_large:
                names = sorted(skipped_large)
                shown = ", ".join(names[:5])
                more = ", ..." if len(names) > 5 else ""
                self.truncations.append(f"{len(names)} files over 2 MB skipped: {shown}{more}")
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
        if len(entries) > LIST_MAX_ENTRIES:
            where = prefix.rstrip("/") or "."
            note = f"list {where}: {LIST_MAX_ENTRIES} of {len(entries)} entries shown"
            self.truncations.append(note)
            return "\n".join([*entries[:LIST_MAX_ENTRIES], f"[truncated: {note}]"])
        return "\n".join(entries) or "(empty)"

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"{path}: not a file")
        if target.stat().st_size > FILE_MAX_BYTES:
            raise ToolError(f"{path}: larger than 2 MB")
        if _is_binary(target):
            raise ToolError(f"{path}: binary file")
        lines = target.read_text(errors="replace").splitlines()
        start = max(start, 1)
        last = min(end or len(lines), len(lines), start + READ_MAX_LINES - 1)
        rel = target.relative_to(self.root).as_posix()
        self.files_read.add(rel)
        out = []
        cut = 0
        for n in range(start, last + 1):
            line = lines[n - 1]
            if len(line) > LINE_MAX_CHARS:
                cut += 1
                out.append(f"{n}: {line[:LINE_MAX_CHARS]} [cut]")
            else:
                out.append(f"{n}: {line}")
        if cut:
            self.truncations.append(f"read {rel}: {cut} lines cut to {LINE_MAX_CHARS} chars")
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
        glob = pathspec.PathSpec.from_lines("gitignore", [path_glob])
        hits = []
        cut = 0
        for rel in self.files():
            if not glob.match_file(rel):
                continue
            text = (self.root / rel).read_text(errors="replace")
            for n, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    if len(line) > LINE_MAX_CHARS:
                        cut += 1
                        hits.append(f"{rel}:{n}: {line[:LINE_MAX_CHARS]} [cut]")
                    else:
                        hits.append(f"{rel}:{n}: {line}")
        if cut:
            self.truncations.append(
                f"grep '{pattern}': {cut} matched lines cut to {LINE_MAX_CHARS} chars"
            )
        if len(hits) > GREP_MAX_MATCHES:
            self.truncations.append(
                f"grep '{pattern}': {GREP_MAX_MATCHES} of {len(hits)} matches shown"
            )
            return "\n".join(hits[:GREP_MAX_MATCHES]) + (
                f"\n[truncated: {GREP_MAX_MATCHES} of {len(hits)} matches shown]"
            )
        return "\n".join(hits) or "(no matches)"


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class TaskWorkspace(Workspace):
    """Walks directory fds with O_NOFOLLOW, so a swapped-in symlink never redirects root's write."""

    def __init__(
        self,
        root: Path,
        exclude: Sequence[str],
        writable: Sequence[str],
        *,
        allow_workflows: bool = False,
    ) -> None:
        super().__init__(root, exclude)
        self.allow_workflows = allow_workflows
        self.writable = frozenset(norm_path(p) for p in writable)
        self.changes: dict[str, str] = {}
        self.originals: dict[str, bytes | None] = {}
        self.writes = 0
        # Taken before any test runs: a test can rewrite a task file, and what it wrote must
        # never be mistaken for the original or carried into the changes.
        self._snapshot: dict[str, bytes | None] = {}
        self._unreadable: dict[str, str] = {}
        for rel in sorted(self.writable):
            try:
                self._snapshot[rel] = self._read(rel, self._check(rel))
            except ToolError as e:
                self._unreadable[rel] = str(e)

    def _check(self, path: str) -> str:
        rel = norm_path(path)
        if rel not in self.writable or rel.startswith(("/", "../")) or rel == "..":
            allowed = ", ".join(sorted(self.writable))
            raise ToolError(f"{path}: not a file of this task; you can write only: {allowed}")
        if self._hard_ignore.match_file(rel) or has_git_component(rel):
            raise ToolError(f"{path}: ignored path")
        if not self.allow_workflows and is_workflow_path(rel):
            raise ToolError(f"{path}: a workflow file, and build.allow_workflow_changes is off")
        return rel

    @staticmethod
    def _refused(path: str, name: str, dir_fd: int, e: OSError, kind: str) -> ToolError:
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            return ToolError(f"{path}: {name}: {type(e).__name__}")
        if stat.S_ISLNK(st.st_mode):
            return ToolError(f"{path}: {name} is a symlink")
        return ToolError(f"{path}: {name} is not a {kind}")

    def _parent(self, path: str, rel: str, create: bool) -> int | None:
        fd = os.open(self.root, _DIR_FLAGS)
        try:
            for name in rel.split("/")[:-1]:
                try:
                    try:
                        child = os.open(name, _DIR_FLAGS, dir_fd=fd)
                    except FileNotFoundError:
                        if not create:
                            os.close(fd)
                            return None
                        os.mkdir(name, 0o755, dir_fd=fd)
                        child = os.open(name, _DIR_FLAGS, dir_fd=fd)
                except OSError as e:
                    raise self._refused(path, name, fd, e, "directory") from e
                os.close(fd)
                fd = child
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        return fd

    def _read(self, path: str, rel: str) -> bytes | None:
        parent = self._parent(path, rel, create=False)
        if parent is None:
            return None
        name = rel.rsplit("/", 1)[-1]
        try:
            try:
                fd = os.open(name, os.O_RDONLY | _FILE_FLAGS, dir_fd=parent)
            except FileNotFoundError:
                return None
            except OSError as e:
                raise self._refused(path, name, parent, e, "regular file") from e
        finally:
            os.close(parent)
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ToolError(f"{path}: not a regular file")
            return f.read()

    def _write(self, path: str, rel: str, content: str) -> str:
        data = content.encode()
        if len(data) > FILE_MAX_BYTES:
            raise ToolError(f"{path}: larger than 2 MB")
        parent = self._parent(path, rel, create=True)
        assert parent is not None
        name = rel.rsplit("/", 1)[-1]
        existed = True
        try:
            try:
                try:
                    fd = os.open(name, os.O_WRONLY | _FILE_FLAGS, dir_fd=parent)
                except FileNotFoundError:
                    existed = False
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS
                    fd = os.open(name, flags, 0o644, dir_fd=parent)
            except OSError as e:
                raise self._refused(path, name, parent, e, "regular file") from e
        finally:
            os.close(parent)
        with os.fdopen(fd, "wb") as f:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise ToolError(f"{path}: not a regular file")
            # Truncating only after this check, so a hard link never lets a write reach the
            # file it shares an inode with.
            if st.st_nlink != 1:
                raise ToolError(f"{path}: a hard link, refused")
            os.ftruncate(fd, 0)
            f.write(data)
            f.flush()
            if existed:
                # Caches keyed on whole-second mtime and size (Python's .pyc) would miss a
                # same-size rewrite within the second, so the mtime always moves a second on.
                now = os.fstat(fd).st_mtime_ns
                later = max(now, st.st_mtime_ns + 1_000_000_000)
                os.utime(fd, ns=(later, later))
        self.originals.setdefault(rel, self._snapshot[rel])
        self.changes[rel] = content
        self.writes += 1
        self._files = None
        return f"wrote {rel} ({len(data)} bytes)"

    def _usable(self, path: str) -> str:
        rel = self._check(path)
        if rel in self._unreadable:
            raise ToolError(self._unreadable[rel])
        return rel

    def original(self, path: str) -> bytes | None:
        """The file's bytes when this workspace was made; None when it did not exist."""
        return self._snapshot[self._usable(path)]

    def write_file(self, path: str, content: str) -> str:
        return self._write(path, self._usable(path), content)

    def edit_file(self, path: str, old: str, new: str) -> str:
        rel = self._usable(path)
        if not old:
            raise ToolError(f"{path}: old text is empty; use write_file for a whole file")
        current = self.changes[rel].encode() if rel in self.changes else self._snapshot[rel]
        if current is None:
            raise ToolError(f"{path}: no such file; use write_file to create it")
        try:
            text = current.decode()
        except UnicodeDecodeError as e:
            raise ToolError(f"{path}: not UTF-8 text; use write_file") from e
        n = text.count(old)
        if n != 1:
            raise ToolError(f"{path}: old text found {n} times; it must match exactly once")
        return self._write(path, rel, text.replace(old, new, 1))
