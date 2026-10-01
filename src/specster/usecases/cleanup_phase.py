"""The cleanup phase: drop a closed pull request's evidence from the evidence branch."""

import shutil
import traceback

from specster.event import Trigger
from specster.evidence_branch import EVIDENCE_BRANCH, EvidenceBranchError, remove
from specster.git import BOT_EMAIL, Author, Git, GitError
from specster.sandbox import scratch_dir
from specster.usecases.context import RunContext, StepOutcome, describe, log, write_outcome

_BRANCH_PREFIX = "specster/issue-"


class CleanupPhase:
    """Never comments: the pull request is closed and the issue has moved on."""

    def __init__(self, run: RunContext, trigger: Trigger) -> None:
        self.run, self.trigger = run, trigger

    def execute(self) -> int:
        env, folder = self.run.env, f"pr-{self.trigger.issue_number}"
        if not self.trigger.head_ref.startswith(_BRANCH_PREFIX):
            log("skipped: not a Specster pull request")
            self._end("skipped")
            return 0
        scratch = scratch_dir()
        try:
            # A throwaway repository: root must never write objects or refs into the checkout.
            repo = scratch / "evidence-repo"
            repo.mkdir()
            git = Git(repo, Author(self.run.cfg.persona.name, BOT_EMAIL), scratch / "git-home")
            git.run("init", "-q", "--template=")
            removed = remove(git, f"{env.server_url}/{env.repo}.git", env.token, folder, scratch)
        except (GitError, EvidenceBranchError, OSError) as e:
            log(f"could not remove {folder} from {EVIDENCE_BRANCH}: {e}")
            self._end("error")
            return 1
        except Exception as e:
            traceback.print_exc()
            log(f"could not remove {folder} from {EVIDENCE_BRANCH}: {describe(e)}")
            self._end("error")
            return 1
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        log(f"removed {folder} from {EVIDENCE_BRANCH}" if removed else f"no {folder} to remove")
        self._end("cleaned" if removed else "skipped")
        return 0

    def _end(self, outcome: StepOutcome) -> None:
        write_outcome(self.run.env, outcome)
        self.run.final = (outcome, None)
