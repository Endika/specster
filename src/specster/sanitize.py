import re
import unicodedata
from dataclasses import dataclass

_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
# Tag characters carry "ASCII smuggling"; unassigned ones in the block are not Cf, so the
# whole block goes, as do both variation-selector ranges (Mn, but invisible on their own).
_INVISIBLE_RANGES = ((0xE0000, 0xE007F), (0xFE00, 0xFE0F), (0xE0100, 0xE01EF))
CODEPOINTS_LISTED = 20


@dataclass(frozen=True)
class Sanitized:
    text: str
    removed: tuple[str, ...]


def _invisible(ch: str) -> bool:
    o = ord(ch)
    return unicodedata.category(ch) == "Cf" or any(lo <= o <= hi for lo, hi in _INVISIBLE_RANGES)


def sanitize(text: str) -> Sanitized:
    removed = [m.group(0) for m in _HTML_COMMENT.finditer(text)]
    text = _HTML_COMMENT.sub("", text)
    kept: list[str] = []
    invisible: list[str] = []
    for ch in text:
        (invisible if _invisible(ch) else kept).append(ch)
    if invisible:
        distinct = sorted({ord(ch) for ch in invisible})
        codes = ", ".join(f"U+{o:04X}" for o in distinct[:CODEPOINTS_LISTED])
        more = len(distinct) - CODEPOINTS_LISTED
        if more > 0:
            codes += f", ...and {more} more"
        removed.append(f"{len(invisible)} invisible characters: {codes}")
        text = "".join(kept)
    return Sanitized(text=text, removed=tuple(removed))
