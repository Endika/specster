"""Build a Node project the way a real build does, with mise installing node, and no model call.

As root inside the image, with network: python -P tests/toolchains_as_slot.py
"""

import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from specster.approved import ApprovedSpec
from specster.build import BuildSetup, run_build
from specster.config import BuildConfig, Config, SkillsConfig
from specster.git import BOT_EMAIL, Author, Git
from specster.ledger import Ledger
from specster.llm.base import ToolCall
from specster.repomap import RepoMap
from specster.sandbox import Sandbox, require_root, scratch_dir, slot_identity
from specster.schemas import PlanTask
from specster.skills import load_skills
from tests.fakes import ScriptBook, ScriptedModel, make_repo
from tests.test_approved import human

FILES = {
    ".nvmrc": "22\n",
    "package.json": '{"name": "fixture", "scripts": {"test": "node test.js"}}\n',
    "app.js": "exports.A = 0;\n",
    "test.js": (
        "const assert = require('node:assert');\n"
        "assert.strictEqual(require('./app.js').A, 1);\n"
        "console.log(`node ${process.version}: ok`);\n"
    ),
}
TASK = PlanTask(id="a", title="A", description="Set A to 1.", files=["app.js"], acceptance=["x"])


def main() -> int:
    require_root(slot_identity(1))
    work = Path(tempfile.mkdtemp(prefix="specster-toolchains-"))
    scratch = scratch_dir()
    try:
        git = make_repo(work / "repo", FILES)
        build = BuildConfig(test_command=["npm", "test"], allow_failing_base=True)
        cfg = Config(build=build)
        skills = load_skills(git.repo, SkillsConfig(), "build", lambda *_: b"", None)
        worker = ScriptBook(
            {
                'id="a"': [
                    [
                        [
                            ToolCall(
                                "w", "write_file", {"path": "app.js", "content": "exports.A = 1;\n"}
                            )
                        ],
                        [
                            ToolCall(
                                "s", "submit_task", {"summary": "ok", "commit_subject": "feat: A"}
                            )
                        ],
                    ]
                ]
            }
        )
        verdict = {"verdict": "approve", "findings": []}
        reviewer = ScriptedModel([[ToolCall("r", "submit_review", verdict)]])

        def sandbox(slot: int) -> Sandbox:
            box = Sandbox(slot_identity(slot), build.test_timeout_s, 20_000, {})
            box.lock_down(git.repo, Git(git.repo, Author("t", BOT_EMAIL), work / "lock"), {})
            return box

        report = run_build(
            BuildSetup(
                cfg,
                ApprovedSpec(human(1, datetime(2026, 1, 1, tzinfo=UTC)), [TASK], "0" * 64, "x"),
                git,
                sandbox,
                Ledger({}),
                lambda: worker,
                lambda: reviewer,
                skills,
                skills,
                RepoMap("", None, 0),
                "specster/issue-1",
                git.head(),
                scratch,
                0.0,
                cfg.persona,
            )
        )
        final = report.final_tests
        print(f"status: {report.status} ({report.reason or 'no reason'})")
        print(final.output if final is not None else "(no final test run)")
        if final is None or report.status != "approved" or not final.ok:
            return 1
        return 0 if "node v22." in final.output else 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
