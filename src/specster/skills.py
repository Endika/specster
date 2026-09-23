import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from specster.config import ALL_PHASES, Phase, SkillsConfig, SkillSource
from specster.repomap import estimate_tokens

Fetch = Callable[[str, Mapping[str, str]], bytes]
AUTODISCOVER_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    ".github/copilot-instructions.md",
    ".cursorrules",
    "CONTRIBUTING.md",
)
AUTODISCOVER_GLOBS = (".cursor/rules/*.mdc", ".github/specster/skills/*.md")
TOKEN_HOSTS = frozenset({"raw.githubusercontent.com", "github.com", "api.github.com"})


class SkillIntegrityError(Exception):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    phases: frozenset[Phase]
    body: str
    origin: str
    verified: bool | None


@dataclass
class SkillBook:
    skills: list[Skill]
    inline: list[Skill]
    on_demand: list[Skill]
    warnings: list[str]
    read: set[str] = field(default_factory=set)

    def get(self, name: str) -> Skill:
        for skill in self.skills:
            if skill.name == name:
                self.read.add(name)
                return skill
        raise KeyError(name)


def http_fetch(url: str, headers: Mapping[str, str]) -> bytes:
    resp = httpx.get(url, headers=dict(headers), timeout=20, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            meta = yaml.safe_load(text[4:end]) or {}
            if isinstance(meta, dict):
                return meta, text[end + 4 :].lstrip("\n")
    return {}, text


def _make(
    text: str, origin: str, stem: str, source: SkillSource | None, verified: bool | None
) -> Skill:
    meta, body = _split_frontmatter(text)
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    phases = source.phase_set() if source else None
    if phases is None:
        raw = meta.get("phases", "all")
        phases = ALL_PHASES if raw == "all" else frozenset(p for p in raw if p in ALL_PHASES)
    return Skill(
        name=str(meta.get("name") or stem),
        description=str(meta.get("description") or first[:120]),
        phases=phases,
        body=body,
        origin=origin,
        verified=verified,
    )


def load_skills(
    root: Path, cfg: SkillsConfig, phase: Phase, fetch: Fetch, auth_token: str | None
) -> SkillBook:
    warnings: list[str] = []
    by_origin: dict[str, Skill] = {}
    if cfg.autodiscover:
        found = [p for p in AUTODISCOVER_FILES if (root / p).is_file()]
        for pattern in AUTODISCOVER_GLOBS:
            found += sorted(
                p.relative_to(root).as_posix() for p in root.glob(pattern) if p.is_file()
            )
        for rel in found:
            by_origin[rel] = _make(
                (root / rel).read_text(errors="replace"), rel, PurePosixPath(rel).stem, None, None
            )
    for source in cfg.sources:
        if source.path is not None:
            by_origin[source.path] = _make(
                (root / source.path).read_text(errors="replace"),
                source.path,
                PurePosixPath(source.path).stem,
                source,
                None,
            )
            continue
        assert source.url is not None
        parsed = urlparse(source.url)
        headers = {}
        if auth_token and parsed.scheme == "https" and parsed.hostname in TOKEN_HOSTS:
            headers["Authorization"] = f"Bearer {auth_token}"
        body = fetch(source.url, headers)
        verified: bool | None = False
        if source.sha256:
            got = hashlib.sha256(body).hexdigest()
            if got != source.sha256:
                raise SkillIntegrityError(
                    f"{source.url}: sha256 mismatch (expected {source.sha256}, got {got})"
                )
            verified = True
        else:
            warnings.append(f"{source.url}: no sha256 pinned")
        by_origin[source.url] = _make(
            body.decode(errors="replace"),
            source.url,
            PurePosixPath(parsed.path).stem,
            source,
            verified,
        )

    skills = list(by_origin.values())
    active = [s for s in skills if phase in s.phases]
    inline: list[Skill] = []
    on_demand: list[Skill] = []
    if cfg.load == "always":
        used = 0
        for skill in active:
            cost = estimate_tokens(skill.body)
            if not on_demand and used + cost <= cfg.max_tokens:
                inline.append(skill)
                used += cost
            else:
                on_demand.append(skill)
        if on_demand:
            names = ", ".join(s.name for s in on_demand)
            warnings.append(f"skills over max_tokens ({cfg.max_tokens}): {names} loaded on demand")
    else:
        on_demand = active
    return SkillBook(skills, inline, on_demand, warnings)
