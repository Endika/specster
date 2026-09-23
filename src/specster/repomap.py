from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

from specster.workspace import Workspace

EXT_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".swift": "swift",
}
_SYMBOL = {
    "function_definition",
    "class_definition",
    "function_declaration",
    "class_declaration",
    "method_definition",
    "method_declaration",
    "interface_declaration",
    "type_alias_declaration",
    "enum_declaration",
    "struct_item",
    "enum_item",
    "trait_item",
    "impl_item",
    "function_item",
    "type_declaration",
    "class",
    "module",
    "method",
    "singleton_method",
    "struct_specifier",
    "object_declaration",
    "protocol_declaration",
}
_WRAPPERS = {"decorated_definition", "export_statement"}
_BODIES = {"block", "class_body", "declaration_list", "body_statement", "interface_body"}
SIG_MAX = 120


def estimate_tokens(text: str) -> int:
    """Rough estimate: ~4 chars per token."""
    return len(text) // 4 + 1


def _signature(node: Node, source: bytes) -> str:
    text = source[node.start_byte : node.end_byte].decode(errors="replace")
    return text.splitlines()[0].strip()[:SIG_MAX]


def _walk(nodes: Sequence[Node], source: bytes, depth: int, out: list[str]) -> None:
    for node in nodes:
        target = node
        if node.type in _WRAPPERS:
            inner = [c for c in node.children if c.type in _SYMBOL]
            if not inner:
                continue
            target = inner[0]
        if target.type not in _SYMBOL:
            continue
        out.append("  " * depth + _signature(node, source))
        if depth == 0:
            for child in target.children:
                if child.type in _BODIES:
                    _walk(child.children, source, 1, out)


def symbols_for(path: str, source: bytes) -> list[str]:
    lang = EXT_LANG.get(PurePosixPath(path).suffix)
    if lang is None:
        return []
    tree = get_parser(lang).parse(source)
    out: list[str] = []
    _walk(tree.root_node.children, source, 0, out)
    return out


@dataclass(frozen=True)
class RepoMap:
    text: str
    truncation: str | None
    file_count: int


def build_repo_map(ws: Workspace, max_tokens: int) -> RepoMap:
    files = ws.files()
    blocks = []
    for rel in files:
        syms = symbols_for(rel, (ws.root / rel).read_bytes())
        blocks.append("\n".join([rel, *("  " + s for s in syms)]))
    full = "\n".join(blocks)
    if estimate_tokens(full) <= max_tokens:
        return RepoMap(full, None, len(files))

    paths = "\n".join(files)
    if estimate_tokens(paths) <= max_tokens:
        return RepoMap(paths, f"symbols omitted for all {len(files)} files", len(files))

    max_depth = max(len(PurePosixPath(f).parts) for f in files) - 1
    for depth in range(max_depth, 0, -1):
        kept = [f for f in files if len(PurePosixPath(f).parts) <= depth]
        cut = Counter(
            "/".join(PurePosixPath(f).parts[:depth])
            for f in files
            if len(PurePosixPath(f).parts) > depth
        )
        lines = kept + [f"\u2026 {n} more files under {d}/" for d, n in sorted(cut.items())]
        text = "\n".join(lines)
        if estimate_tokens(text) <= max_tokens:
            return RepoMap(
                text,
                f"symbols omitted; paths deeper than depth {depth} summarized",
                len(files),
            )

    top = Counter(PurePosixPath(f).parts[0] for f in files)
    text = "\n".join(f"\u2026 {n} files under {d}" for d, n in sorted(top.items()))
    return RepoMap(text, "only top-level directory counts fit (depth 0)", len(files))
