"""Before/after evidence between two commits: each side exported, set up, served and captured."""

import json
import math
import os
import shutil
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import httpx

from specster.browser import INSTALL_MAX_S, SCRIPT, BrowserEnv, Installer, install
from specster.config import Config, PreviewConfig
from specster.evidence import (
    LOG_TAIL_CHARS,
    PAGE_MAX_HEIGHT,
    PAGE_MAX_S,
    EvidenceRun,
    Shot,
    Side,
    collect,
    read_shots,
)
from specster.git import BOT_EMAIL, Author, Git
from specster.sandbox import Sandbox, Server
from specster.schemas import EvidencePage, EvidenceRequest
from specster.telemetry import span


def enclose(git: Git, author: str, sandbox: Sandbox, enclosure: Path, commit: str) -> Path:
    """A credential-less repo, never a worktree: once handed over, root never runs git in it."""
    enclosure.mkdir(mode=0o750)
    enclosure.chmod(0o750)
    if sandbox.identity is not None:
        os.chown(enclosure, -1, sandbox.identity.gid, follow_symlinks=False)
    tree = enclosure / "tree"
    tree.mkdir()
    git.export_tree(commit, tree, enclosure / "index")
    Git(tree, Author(author, BOT_EMAIL), enclosure / "git-home").seed(f"specster: {commit}")
    return tree


@dataclass(frozen=True)
class CaptureSetup:
    cfg: Config
    git: Git
    scratch: Path
    # Who the seed commit of each exported side is by.
    author: str
    # Seconds left before the run's time limit; None never stops on time.
    time_left: Callable[[], float] | None = None
    install_browser: Installer = install
    # What the warnings call the run that is out of time.
    run_name: str = "the build"


class EvidenceCapture:
    """Runs the requests and shoots the pages at a base and a head commit, in one sandbox.

    What it cuts or skips goes to the `truncations` and `warnings` lists it is given.
    """

    def __init__(
        self,
        setup: CaptureSetup,
        sandbox: Callable[[], Sandbox],
        truncations: list[str],
        warnings: list[str],
    ) -> None:
        self.s = setup
        self._sandbox = sandbox
        self.truncations = truncations
        self.warnings = warnings

    def _out_of_time(self) -> bool:
        left = self.s.time_left
        return left is not None and left() <= 0

    def collect(
        self,
        base: str,
        head: str,
        requests: Sequence[EvidenceRequest],
        pages: Sequence[EvidencePage],
    ) -> EvidenceRun | None:
        """The requests and pages at `base` and `head`; never raises, None when nothing ran."""
        with span("evidence"):
            return self._collect(base, head, requests, pages)

    def _collect(
        self,
        base: str,
        head: str,
        requests: Sequence[EvidenceRequest],
        pages: Sequence[EvidencePage],
    ) -> EvidenceRun | None:
        preview = self.s.cfg.build.preview
        if not requests and not pages:
            return None
        if preview is None:
            self.warnings.append(
                "the spec lists evidence, but build.preview is not set: none collected"
            )
            return None
        if self._out_of_time():
            self.warnings.append(f"evidence skipped: {self.s.run_name} is out of time")
            return None
        browser = self._install_browser() if pages else None
        if pages and browser is None and not requests:
            return None
        enclosures: list[Path] = []
        failed: dict[str, str] = {}
        homes: dict[Side, Path] = {}
        commits: dict[Side, str] = {"base": base, "head": head}
        try:
            sandbox = self._sandbox()

            def start_side(side: Side) -> tuple[Server | None, str | None]:
                return self._start_side(
                    sandbox, preview, side, commits[side], enclosures, failed, homes
                )

            def shoot(side: Side) -> tuple[dict[str, tuple[Shot, ...]], str]:
                assert browser is not None and preview is not None
                return self._shoot(sandbox, browser, preview, pages, side, homes[side])

            # The app is on loopback: an HTTP(S)_PROXY from the runner must not catch it.
            with httpx.Client(trust_env=False) as client:
                run = collect(
                    start_side,
                    preview,
                    requests,
                    client,
                    self.s.time_left or _forever,
                    pages=pages,
                    shoot=shoot if browser is not None else None,
                )
        except Exception as e:
            traceback.print_exc()
            self.warnings.append(f"evidence: {type(e).__name__}: {e}")
            return None
        finally:
            for enclosure in enclosures:
                shutil.rmtree(enclosure, ignore_errors=True)
        problems = tuple(
            replace(p, log_tail=failed[p.side][-LOG_TAIL_CHARS:]) if p.side in failed else p
            for p in run.problems
        )
        return replace(run, problems=problems, logs={**run.logs, **failed})

    def _install_browser(self) -> BrowserEnv | None:
        """Once per run, as root; a failure only costs the screenshots."""
        left = self.s.time_left() if self.s.time_left is not None else INSTALL_MAX_S
        try:
            with span("browser install"):
                got = self.s.install_browser(min(INSTALL_MAX_S, left))
        except Exception as e:
            traceback.print_exc()
            got = f"the browser install failed: {type(e).__name__}: {e}"
        if isinstance(got, str):
            self.warnings.append(f"screenshots skipped: {got}")
            return None
        return got

    def _shoot(
        self,
        sandbox: Sandbox,
        browser: BrowserEnv,
        preview: PreviewConfig,
        pages: Sequence[EvidencePage],
        side: Side,
        home: Path,
    ) -> tuple[dict[str, tuple[Shot, ...]], str]:
        """Shot as the slot while the side's server runs; the server's stop reaps the browser."""
        out = home / "shots"
        argv = [
            "/usr/bin/env",
            f"PLAYWRIGHT_BROWSERS_PATH={browser.browsers}",
            str(browser.python),
            "-I",
            str(SCRIPT),
            preview.origin,
            str(out),
            json.dumps([{"name": p.name, "path": p.path} for p in pages]),
            str(PAGE_MAX_HEIGHT),
            f"{PAGE_MAX_S:g}",
        ]
        res = sandbox.run(argv, home, home, f"browser {side}", reap=False)
        if res.truncation:
            self.truncations.append(res.truncation)
        log = res.output
        if res.timed_out:
            log += f"\nbrowser {side}: timed out\n"
        return read_shots(out, pages, side), log

    def _start_side(
        self,
        sandbox: Sandbox,
        preview: PreviewConfig,
        side: Side,
        commit: str,
        enclosures: list[Path],
        failed: dict[str, str],
        homes: dict[Side, Path],
    ) -> tuple[Server | None, str | None]:
        """The side's server, started after the setup and seed commands; else why not."""
        enclosure = self.s.scratch / f"evidence-{side}"
        enclosures.append(enclosure)
        tree = enclose(self.s.git, self.s.author, sandbox, enclosure, commit)
        sandbox.hand_over(tree)
        home = homes[side] = sandbox.new_home(enclosure, "home")
        steps = [
            ("setup_command", self.s.cfg.build.setup_command),
            ("seed_command", preview.seed_command),
        ]
        for which, argv in steps:
            if argv is None:
                continue
            res = sandbox.run(argv, tree, home, f"{which.removesuffix('_command')} {side}")
            if res.truncation:
                self.truncations.append(res.truncation)
            if not res.ok:
                failed[side] = res.output
                timed_out = ", timed out" if res.timed_out else ""
                return None, f"{which} failed (exit {res.exit_code}{timed_out})"
        return sandbox.start(preview.serve_command, tree, home, f"serve {side}"), None


def _forever() -> float:
    return math.inf
