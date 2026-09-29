"""Run a real build of an approved plan on the fixture repo, as root in the image, and grade it.

Everything is real except GitHub: sandboxes, git, test commands and models. The grade is a
hidden test the worker never saw, run as a sandbox slot on the branch the build produced.
"""

import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from specster.approved import ApprovedSpec
from specster.build import BuildReport, BuildSetup, run_build
from specster.config import BuildConfig, Config, ModelConfig, SkillsConfig, load_config
from specster.git import BOT_EMAIL, Author, Git
from specster.github import Comment
from specster.ledger import Ledger
from specster.llm.base import ChatModel
from specster.llm.factory import build_chat_model
from specster.repomap import build_repo_map
from specster.sandbox import RunResult, Sandbox, scratch_dir, slot_identity
from specster.schemas import PlanTask
from specster.skills import SkillBook, http_fetch, load_skills
from specster.workspace import Workspace

EVALS = Path(__file__).parent
FIXTURE = EVALS / "fixtures" / "build_repo"
CHECKS = EVALS / "fixtures" / "build_checks"
CASES = EVALS / "build_cases.yaml"
REPO_CONFIG = EVALS.parent / ".github" / "specster" / "config.yml"
BUILD = BuildConfig(
    setup_command=["uv", "sync", "--quiet"],
    test_command=["uv", "run", "--quiet", "pytest", "-q", "-p", "no:cacheprovider"],
    max_parallel=2,
)
REVIEWER = ModelConfig(provider="anthropic", model="claude-opus-5-5", effort="medium")
HIDDEN_SLOT = 8


@dataclass(frozen=True)
class BuildCase:
    id: str
    spec: str
    tasks: list[PlanTask]
    hidden: str


@dataclass(frozen=True)
class BuildOutcome:
    report: BuildReport
    hidden: RunResult
    cost: float | None
    turns: int
    seconds: float


def load_build_cases() -> list[BuildCase]:
    raw: list[dict[str, Any]] = yaml.safe_load(CASES.read_text())
    return [
        BuildCase(c["id"], c["spec"], [PlanTask.model_validate(t) for t in c["tasks"]], c["hidden"])
        for c in raw
    ]


def skill_books(repo: Path, with_skills: bool) -> tuple[SkillBook, SkillBook]:
    """This repository's own build and review skills, or none at all."""
    skills = load_config(REPO_CONFIG).skills if with_skills else SkillsConfig(autodiscover=False)
    build = load_skills(repo, skills, "build", http_fetch, None)
    review = load_skills(repo, skills, "review", http_fetch, None)
    return build, review


def _repo(root: Path) -> Git:
    repo = Path(shutil.copytree(FIXTURE, root))
    git = Git(repo, Author("Specster", BOT_EMAIL), root.parent / "git-home")
    git.run("init", "-q", "--initial-branch=main")
    git.run("add", "-A")
    git.run("commit", "-q", "-m", "chore: init")
    return git


def _hidden_check(git: Git, head: str, hidden: str, scratch: Path) -> RunResult:
    """Run the hidden test as a slot on a fresh copy of the branch the build produced."""
    box = Sandbox(slot_identity(HIDDEN_SLOT), BUILD.test_timeout_s, 20_000, {})
    box.lock_down(git.repo, git, {})
    enclosure = scratch / "hidden"
    enclosure.mkdir(mode=0o750)
    enclosure.chmod(0o750)
    identity = box.identity
    assert identity is not None
    os.chown(enclosure, -1, identity.gid)
    tree = enclosure / "tree"
    tree.mkdir()
    git.export_tree(head, tree, enclosure / "index")
    shutil.copy(CHECKS / hidden, tree / "tests" / hidden)
    box.hand_over(tree)
    home = box.new_home(enclosure, "home")
    assert BUILD.setup_command is not None
    setup = box.run(BUILD.setup_command, tree, home, "hidden setup")
    if not setup.ok:
        return setup
    return box.run(
        ["uv", "run", "--quiet", "pytest", "-q", "-p", "no:cacheprovider", f"tests/{hidden}"],
        tree,
        home,
        "hidden check",
    )


def run_build_case(case: BuildCase, worker: ModelConfig, with_skills: bool) -> BuildOutcome:
    work = Path(tempfile.mkdtemp(prefix="specster-bench-"))
    scratch = scratch_dir()
    started = time.monotonic()
    try:
        git = _repo(work / "repo")
        cfg = Config(build=BUILD)
        build_skills, review_skills = skill_books(git.repo, with_skills)
        ledger = Ledger({})
        at = datetime.now(UTC)
        comment = Comment(1, "specster[bot]", "Bot", "NONE", case.spec, at, at)

        def make(model: ModelConfig) -> ChatModel:
            return build_chat_model(model, os.environ)

        def sandbox(slot: int) -> Sandbox:
            box = Sandbox(slot_identity(slot), BUILD.test_timeout_s, 20_000, {})
            box.lock_down(git.repo, git, {})
            return box

        repo_map = build_repo_map(Workspace(git.repo, cfg.repo_map.exclude), 2_000)
        report = run_build(
            BuildSetup(
                cfg.model_copy(update={"models": cfg.models.model_copy(update={"worker": worker})}),
                ApprovedSpec(comment, case.tasks, "0" * 64, case.spec),
                git,
                sandbox,
                ledger,
                lambda: make(worker),
                lambda: make(REVIEWER),
                build_skills,
                review_skills,
                repo_map,
                "specster/issue-1",
                git.head(),
                scratch,
                0.0,
                cfg.persona,
            )
        )
        hidden = _hidden_check(git, report.head, case.hidden, scratch)
        return BuildOutcome(
            report, hidden, ledger.cost(), ledger.turns(), round(time.monotonic() - started, 1)
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
