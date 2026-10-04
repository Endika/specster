"""The evidence phase: before/after evidence between a pull request's base and head."""

import dataclasses
import secrets
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from specster.agent import (
    NO_SUBMISSION,
    TIME_UP,
    AgentError,
    Deadline,
    Metered,
    run_evidence_planner,
)
from specster.approved import BuildRefused
from specster.browser import Installer, install
from specster.build import FINAL_SLOT, bill_fatal, budget_cut, budget_stop, install_tools
from specster.config import ModelConfig
from specster.event import Trigger
from specster.evidence import LOG_TAIL_CHARS, EvidenceRun
from specster.evidence import files as evidence_files
from specster.evidence_branch import EVIDENCE_BRANCH, EvidenceBranchError, publish
from specster.evidence_run import CaptureSetup, EvidenceCapture
from specster.git import BOT_EMAIL, Author, Git, GitError
from specster.github import GitHubError, PullFiles, PullInfo, PullTracker
from specster.ledger import Ledger
from specster.llm.base import ChatModel
from specster.llm.factory import ProviderConfigError
from specster.metrics import RunMetrics
from specster.prompts import context_block, evidence_planner_prompt, pull_block
from specster.render import PullEvidenceView, hint, render_pull_evidence
from specster.repomap import build_repo_map
from specster.sandbox import (
    Identity,
    Sandbox,
    SandboxError,
    lock_down,
    scratch_dir,
)
from specster.schemas import EvidencePlan
from specster.skills import Fetch
from specster.thread import HiddenItem
from specster.usecases.build_phase import give_back
from specster.usecases.context import (
    LABEL_COLORS,
    NO_CHECKOUT,
    PROVIDER_HINT,
    Failure,
    Outcome,
    RunContext,
    describe,
    log,
    usage_fields,
)
from specster.usecases.pull_request import check_host, known_spend
from specster.workspace import Workspace

PLANNER = "planner"


@dataclass
class _Planned:
    """What the evidence planner chose, and what reading the pull request left out."""

    plan: EvidencePlan | None
    files_read: list[str]
    hidden: list[HiddenItem]
    truncations: list[str]
    # Why the planner stopped short: a budget cap or the time limit.
    budget: str = ""
    out_of_time: bool = False


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
        self.lang = run.cfg.persona.language
        self.label = run.cfg.labels.evidence
        self.warnings: list[str] = []
        self.truncations: list[str] = []
        # Set once root may have written to the checkout's .git, so cleanup hands it back.
        self._touched = False

    def execute(self) -> int:
        run = self.run
        cfg, labels = run.cfg, run.cfg.labels
        run.tracker.ensure_labels(
            {
                labels.evidence: LABEL_COLORS["evidence"],
                labels.needs_human: LABEL_COLORS["needs_human"],
            }
        )
        if cfg.build.preview is None:
            return run.refuse(
                BuildRefused(
                    "build.preview is not set, so Specster cannot serve the app to capture it",
                    hint(
                        self.lang,
                        "hint_evidence_preview",
                        path=run.env.config_path,
                        label=self.label,
                    ),
                )
            )
        known = known_spend(run)
        if known is None:
            return 0
        check_host(run, self.identity)
        model = self._planner_model()
        files = self._files()
        scratch = scratch_dir()
        try:
            return self._run(self._git(scratch), scratch, model, files, known)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            if self._touched:
                give_back(run.env.workspace)

    def _planner_model(self) -> ChatModel:
        try:
            return self.make_model(self.run.cfg.models.planner)
        except ProviderConfigError as e:
            raise Failure(describe(e), PROVIDER_HINT) from e

    def _files(self) -> PullFiles:
        try:
            return self.pulls.pull_files(self.run.number)
        except (GitHubError, httpx.HTTPError) as e:
            raise Failure(
                f"could not read the files of pull request #{self.run.number}: {describe(e)}",
                hint(self.lang, "hint_pull_read", label=self.label),
            ) from e

    def _git(self, scratch: Path) -> Git:
        """The checkout's git with the pull request's base and head in it, then locked down."""
        run, pull = self.run, self.pull
        env = run.env
        git = Git(env.workspace, Author(run.cfg.persona.name, BOT_EMAIL), scratch / "git-home")
        try:
            git.head()
        except GitError as e:
            raise Failure(NO_CHECKOUT, hint(self.lang, "hint_checkout")) from e
        self._touched = True
        try:
            git.fetch_commits(
                f"{env.server_url}/{env.repo}.git", env.token, [pull.base_sha, pull.head_sha]
            )
        except GitError as e:
            raise Failure(
                f"could not fetch the commits of pull request #{pull.number}: {e}",
                hint(self.lang, "hint_pull_fetch", label=self.label),
            ) from e
        for line in lock_down(env.workspace, git, env.secrets):
            log(f"lock down: {line}")
        return git

    def _time_left(self) -> Callable[[], float]:
        run = self.run
        deadline = run.started + run.cfg.build.max_minutes * 60
        return lambda: deadline - run.timer()

    def _run(
        self, git: Git, scratch: Path, model: ChatModel, files: PullFiles, known: float
    ) -> int:
        run, pull = self.run, self.pull
        cfg = run.cfg
        ledger = run.ledger = Ledger(cfg.pricing)
        time_left = self._time_left()
        planned = self._plan(git, scratch, model, files, ledger, known, time_left)
        plan = planned.plan
        evidence: EvidenceRun | None = None
        reason = planned.budget
        if plan is not None and (plan.evidence or plan.pages):
            try:
                evidence, reason = self._capture(git, scratch, plan, time_left)
            except SandboxError as e:
                raise Failure(
                    describe(e),
                    hint(self.lang, "hint_sandbox", label=self.label),
                    needs_human=True,
                ) from e
        outcome: Outcome = "budget_exhausted" if planned.budget else "evidence_posted"
        m = self._metrics(outcome, ledger, planned, evidence)
        view = PullEvidenceView(
            pull.base_sha,
            pull.head_sha,
            self.label,
            plan,
            evidence,
            reason=reason,
            budget_spent=bool(planned.budget),
            out_of_time=planned.out_of_time,
        )
        if evidence is not None and (evidence.items or evidence.pages):
            view = self._upload(view, git, evidence, scratch)
        ctx = dataclasses.replace(run.context(m), hidden=tuple(planned.hidden))
        run.finish(outcome, render_pull_evidence(view, ctx), [self.label], metrics=m)
        return 0

    def _plan(
        self,
        git: Git,
        scratch: Path,
        model: ChatModel,
        files: PullFiles,
        ledger: Ledger,
        known: float,
        time_left: Callable[[], float],
    ) -> _Planned:
        """Ask the evidence planner, which reads an export of the head, never the checkout."""
        run, pull = self.run, self.pull
        cfg = run.cfg
        preview = cfg.build.preview
        assert preview is not None
        head = scratch / "head"
        head.mkdir()
        git.export_tree(pull.head_sha, head, scratch / "head.index")
        ws = Workspace(head, cfg.repo_map.exclude)
        repo_map = build_repo_map(ws, cfg.repo_map.max_tokens)
        user, hidden, cuts = pull_block(pull, files, secrets.token_hex(8))
        truncations = [*([repo_map.truncation] if repo_map.truncation else []), *cuts]
        planner = cfg.models.planner
        meter = ledger.meter(planner)

        def stop() -> str | None:
            return budget_stop(ledger, cfg.budget, known)

        try:
            out = run_evidence_planner(
                Deadline(Metered(model, meter, stop), time_left),
                evidence_planner_prompt(cfg.persona, preview),
                context_block(repo_map, ()),
                user,
                ws,
                cfg.budget.max_turns,
            )
        except AgentError as e:
            ledger.add(PLANNER, planner, e.usage, e.turns)
            read, cut = sorted(ws.files_read), truncations + ws.truncations
            if TIME_UP in str(e):
                late = f"time limit reached: build.max_minutes is {cfg.build.max_minutes}"
                return _Planned(None, read, hidden, cut, late, out_of_time=True)
            cap = budget_cut(str(e))
            if cap is None and str(e).startswith(NO_SUBMISSION):
                raise Failure(
                    f"evidence planner: {e}",
                    hint(self.lang, "hint_evidence_planner", label=self.label),
                ) from e
            if cap is None:
                raise Failure(describe(e), PROVIDER_HINT) from e
            return _Planned(None, read, hidden, cut, cap)
        except BaseException as e:
            bill_fatal(ledger, PLANNER, planner, e, meter)
            raise
        finally:
            meter.close()
        ledger.add(PLANNER, planner, out.usage, out.turns)
        return _Planned(out.value, sorted(ws.files_read), hidden, truncations + ws.truncations)

    def _capture(
        self, git: Git, scratch: Path, plan: EvidencePlan, time_left: Callable[[], float]
    ) -> tuple[EvidenceRun | None, str]:
        """The plan run at the base and the head, and why it did not run if it did not."""
        run, pull = self.run, self.pull
        cfg, env = run.cfg, run.env
        build = cfg.build
        # Made before anything else, so a sandbox that cannot be locked down fails the run.
        box = Sandbox(
            self.identity(FINAL_SLOT),
            build.test_timeout_s,
            build.test_output_max_kb * 1024,
            build.test_env,
            max_file_bytes=build.test_output_max_file_mb * 1024 * 1024,
            time_left=time_left,
        )
        box.lock_down(env.workspace, git, env.secrets)

        def sandbox() -> Sandbox:
            return box

        author = cfg.persona.name
        failed = install_tools(
            git, build, pull.base_sha, sandbox, [], scratch, author, self.truncations, self.warnings
        )
        if failed is not None:
            tail = " ".join(failed.output[-LOG_TAIL_CHARS // 8 :].split())
            return None, f"Nothing was captured: the toolchains could not be installed: {tail}"
        capture = EvidenceCapture(
            CaptureSetup(
                cfg, git, scratch, author, time_left, self.install_browser, "the evidence run"
            ),
            sandbox,
            self.truncations,
            self.warnings,
        )
        return capture.collect(pull.base_sha, pull.head_sha, plan.evidence, plan.pages), ""

    def _metrics(
        self,
        outcome: Outcome,
        ledger: Ledger,
        planned: _Planned,
        evidence: EvidenceRun | None,
    ) -> RunMetrics:
        run = self.run
        unpriced = [
            f"{role} model has no price: the budget caps do not count it"
            for role in ledger.unpriced()
        ]
        facts: dict[str, Any] = {
            "files_read": planned.files_read,
            "hidden_removed": len(planned.hidden),
            "evidence_items": len(evidence.items) if evidence else 0,
            "evidence_pages": len(evidence.pages) if evidence else 0,
            "evidence_problems": len(evidence.problems) if evidence else 0,
            "truncations": planned.truncations + self.truncations,
            "warnings": run.warnings + self.warnings + unpriced,
        }
        return run.metrics(
            outcome,
            ledger.cost(),
            roles=ledger.roles(),
            turns=ledger.turns(),
            **facts,
            **usage_fields(ledger.usage()),
        )

    def _upload(
        self, view: PullEvidenceView, git: Git, evidence: EvidenceRun, scratch: Path
    ) -> PullEvidenceView:
        """Publish the evidence files under `pr-<N>`; a failure is said, never fatal."""
        env = self.run.env
        folder = f"pr-{self.run.number}"
        files = evidence_files(evidence)
        try:
            sha = publish(
                git,
                f"{env.server_url}/{env.repo}.git",
                env.token,
                folder,
                files,
                scratch / "evidence-branch",
            )
        except (GitError, EvidenceBranchError, GitHubError, OSError) as e:
            log(f"could not upload the evidence files: {e}")
            note = hint(self.lang, "evidence_upload_failed", why=str(e))
            return dataclasses.replace(view, note=note)
        links = {"folder": f"{env.server_url}/{env.repo}/tree/{EVIDENCE_BRANCH}/{folder}"}
        # At the published commit, so a later run's screenshots never replace these.
        links |= {
            name: f"{env.server_url}/{env.repo}/blob/{sha}/{folder}/{name}?raw=true"
            for name in files
            if name.endswith(".png")
        }
        return dataclasses.replace(view, links=links)
