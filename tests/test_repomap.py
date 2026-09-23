from pathlib import Path

from specster.repomap import build_repo_map, symbols_for
from specster.workspace import Workspace


def test_python_symbols_include_classes_methods_and_functions() -> None:
    src = (
        b"class Report:\n"
        b"    def to_csv(self, sep: str) -> str:\n"
        b"        return ''\n"
        b"\n"
        b"def load(path):\n"
        b"    pass\n"
    )
    assert symbols_for("a.py", src) == [
        "class Report:",
        "  def to_csv(self, sep: str) -> str:",
        "def load(path):",
    ]


def test_typescript_exported_function_is_found() -> None:
    src = b"export function exportCsv(rows: Row[]): string {\n  return '';\n}\n"
    assert symbols_for("a.ts", src) == ["export function exportCsv(rows: Row[]): string {"]


def test_unknown_language_has_no_symbols() -> None:
    assert symbols_for("notes.txt", b"hello") == []


def test_small_repo_fits_with_symbols(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    m = build_repo_map(Workspace(tmp_path), max_tokens=1000)
    assert m.text == "app.py\n  def main():"
    assert m.truncation is None


def test_over_budget_drops_symbols_and_says_so(tmp_path: Path) -> None:
    for i in range(30):
        (tmp_path / f"m{i}.py").write_text("".join(f"def f{j}():\n    pass\n" for j in range(20)))
    m = build_repo_map(Workspace(tmp_path), max_tokens=150)
    assert "def f0" not in m.text
    assert m.truncation is not None and "symbols omitted" in m.truncation


def test_far_over_budget_cuts_deep_paths_and_counts_them(tmp_path: Path) -> None:
    deep = tmp_path / "pkg" / "deep" / "deeper"
    deep.mkdir(parents=True)
    for i in range(200):
        (deep / f"file_{i:03}.py").write_text("x = 1\n")
    m = build_repo_map(Workspace(tmp_path), max_tokens=40)
    assert "more files under" in m.text
    assert m.truncation is not None and "depth" in m.truncation
