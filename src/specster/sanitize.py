import re
from dataclasses import dataclass

_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


@dataclass(frozen=True)
class Sanitized:
    text: str
    removed: tuple[str, ...]


def sanitize(text: str) -> Sanitized:
    removed = [m.group(0) for m in _HTML_COMMENT.finditer(text)]
    text = _HTML_COMMENT.sub("", text)
    invisible = _INVISIBLE.findall(text)
    if invisible:
        codes = ", ".join(sorted({f"U+{ord(c):04X}" for c in invisible}))
        removed.append(f"{len(invisible)} invisible characters: {codes}")
        text = _INVISIBLE.sub("", text)
    return Sanitized(text=text, removed=tuple(removed))
