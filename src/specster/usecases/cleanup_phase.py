"""The cleanup phase: drop a closed pull request's evidence from the evidence branch."""

import shutil
import traceback

from specster.event import Trigger
from specster.evidence_branch import EVIDENCE_BRANCH, EvidenceBranchError, remove
from specster.git import BOT_EMAIL, Author, Git, GitError
from specster.sandbox import scratch_dir
from specster.usecases.context import RunContext, describe, log, write_outcome

_BRANCH_PREFIX = "specster/issue-"


class CleanupPhase:
    """Never comments: the pull request is closed and the issue has moved on."""

    def __init__(self, run: RunContext, trigger: Trigger) -> None:
        self.run, self.trigger = run, trigger

    def execute(self) -> int:
        env, folder = self.run.env, f"pr-{self.trigger.issue_number}"
        if not self.trigger.head_ref.startswith(_BRANCH_PREFIX):
            log("skipped: not a Specster pull request")
            write_outcome(env, "skipped")
            return 0
        scratch = scratch_dir()
        try:
            git = Git(
                env.workspace, Author(self.run.cfg.persona.name, BOT_EMAIL), scratch / "git-home"
            )
            removed = remove(git, f"{env.server_url}/{env.repo}.git", env.token, folder, scratch)
        except (GitError, EvidenceBranchError, OSError) as e:
            log(f"could not remove {folder} from {EVIDENCE_BRANCH}: {e}")
            write_outcome(env, "error")
            return 1
        except Exception as e:
            traceback.print_exc()
            log(f"could not remove {folder} from {EVIDENCE_BRANCH}: {describe(e)}")
            write_outcome(env, "error")
            return 1
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        log(f"removed {folder} from {EVIDENCE_BRANCH}" if removed else f"no {folder} to remove")
        write_outcome(env, "cleaned" if removed else "skipped")
        return 0
