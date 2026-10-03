"""The evidence phase: before/after evidence between a pull request's base and head."""

from collections.abc import Callable

from specster.approved import BuildRefused
from specster.browser import Installer, install
from specster.config import ModelConfig
from specster.event import Trigger
from specster.github import PullInfo, PullTracker
from specster.llm.base import ChatModel
from specster.render import hint
from specster.sandbox import Identity
from specster.skills import Fetch
from specster.usecases.context import RunContext


class EvidencePhase:
    """`ai-evidence` on an open, ready pull request from this repository."""

    def __init__(
        self,
        run: RunContext,
        trigger: Trigger,
        pull: PullInfo,
        pulls: PullTracker,
        make_model: Callable[[ModelConfig], ChatModel],
        fetch: Fetch,
        identity: Callable[[int], Identity | None],
        install_browser: Installer = install,
    ) -> None:
        self.run = run
        self.trigger = trigger
        self.pull = pull
        self.pulls = pulls
        self.make_model = make_model
        self.fetch = fetch
        self.identity = identity
        self.install_browser = install_browser

    def execute(self) -> int:
        lang = self.run.cfg.persona.language
        return self.run.refuse(
            BuildRefused(
                f"{self.run.cfg.labels.evidence} is not implemented yet",
                hint(lang, "hint_not_implemented"),
            )
        )
