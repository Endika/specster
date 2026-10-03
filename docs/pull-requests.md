# Specster on any pull request

Two labels work on a pull request, whoever opened it, as long as its branch is in the same
repository: `ai-evidence` shows what the pull request changes in the running app, and `ai-fix`
applies its open review comments as commits on its branch. Neither needs an issue, a spec or an
earlier Specster run. The label names are `labels.evidence` and `labels.fix`; the workflow jobs
are in [The workflow, line by line](configuration.md#the-workflow-line-by-line).

## `ai-evidence`

Reads the pull request's title, description, changed files and diff, and the repository at the
pull request's head. It reads no comment and no review. A planner model picks the requests and
pages that show the change, then Specster does what an approved build does: it starts the app at
the base commit and at the head, sends each request, screenshots each page, and comments the
before and after on the pull request. It never writes the pull request's description. The full
responses go to `pr-<N>/` on the `specster-evidence` branch, as described in
[Before/after evidence](build.md#beforeafter-evidence).

- It needs `build.preview`. Without it the run is refused, before any model call.
- The comment says what the planner chose and why. When nothing in the running app changes
  (tests, docs, CI, a refactor) the plan is empty: no app is started and the comment says why
  there was nothing to capture.
- The toolchains (`build.tools` or what mise reads from the repository) are installed from the
  pull request's base commit and used for both sides. A pull request that bumps a tool version is
  therefore served at its head with the base's toolchain.
- The planner gets no project skills, and no pull request comment.
- Adding the label again captures anew and replaces `pr-<N>`.

## `ai-fix`

Reads the pull request's open review threads and the summaries of its reviews (changes requested
or commented, with a body). Resolved threads and approvals are skipped. A planner turns what the
reviewers asked into tasks, and the build machinery applies them: workers, tests in the sandbox,
a reviewer, the same `build.*` settings, escalation included. The result is pushed as commits on
the pull request's own branch, never to a new branch.

- A pull request whose head is the default branch, `specster-evidence` or a protected branch (classic
  branch protection, or a ruleset that requires pull requests or restricts updates; a `main` to
  `release` pull request, say) is refused before any model call: the label needs only
  triage, and its commits would land there without a review.

- Only comments from people `trust.comments` accepts count, plus the pull request's author with
  `trust.issue_author`. Comments written or edited after the label (with
  `trust.snapshot_at_label`) are left out and counted in the comment.
- A thread is read as of its last comment. Specster's own earlier replies in it are context only.
- The planner may decline an item (a question, a contradiction, already done, outside the pull
  request), with a reason. It cannot plan a change under `.github/workflows`, `.github/actions` or
  Specster's configuration unless `build.allow_workflow_changes` or `build.allow_config_changes`
  allows it. `build.close_issue` does not apply. `build.allow_failing_base` applies with the
  head as the base.
- The planner gets no project skills; the workers and the reviewer get the build and review ones
  from the default branch.

### What Specster posts

On a push, each thread gets a reply: `Applied in <sha>.` (the commits of the tasks that address
it) or `Not applied: <reason>`. A thread is never resolved: the reviewer decides. Then one summary
comment on the pull request: the branch, the old and new head, a table of items (linked to their
thread or review), the tasks, the tests and the cost footer. A review summary has no thread, so
it is listed in the comment only.

When the planner declines every item, nothing is pushed: each thread still gets its
`Not applied` reply, the summary says `Nothing to change`, and the run ends `refused`.

If the reviewer does not approve, or the build fails, nothing is pushed and no thread is replied
to. The summary lists the tasks, the pending findings and the reasons, and the run ends
`not_approved` or `build_failed`. These outcomes add no `needs-human`: a person is already
reviewing the pull request. A run that ends `error` on a sandbox error, in either phase, does
add `needs-human`: the runner needs fixing before any label can work.

### Re-runs

Add `ai-fix` again after more review. A thread already answered is skipped until a trusted
comment newer than Specster's last reply appears in it. A review summary is skipped once a
Specster summary marks it answered, which only a push or a run that declined every item does.
A summary that ends `not_approved`, `build_failed`, `budget_exhausted`, or `refused` because the
pull request moved or closed, lists its items without marking them, so the next run reads them
again. An answered summary comes back if it is edited, and an edited review comment counts
again. Items Specster hid or left out say so in the comment.

Specster tells its own thread replies apart by its login. When `identity.bot_login` is not set
and Specster cannot read the token's own login, its
earlier replies count as someone else's comments (untrusted, under the default trust settings),
while the summary's marker still decides what is skipped. Set `identity.bot_login`
(`github-actions[bot]` for the default token), or run as your
[own bot](configuration.md#your-own-bot-identity), so they are recognized.

## Outcomes

| Outcome | When |
|---|---|
| `evidence_posted` | the evidence comment was posted, an empty plan included |
| `fix_pushed` | the review was applied and pushed |
| `refused` | a fork or deleted fork, a closed or draft pull request; `ai-evidence` without `build.preview`; `ai-fix` with nothing to apply, every item declined, a head it never pushes to, or a moved pull request |
| `not_approved`, `build_failed` | `ai-fix` only: the build did not pass its review or tests |
| `budget_exhausted` | the budget or the time limit stopped the run |
| `error` | the pull request could not be read, a fetch or push failed, the sandbox did not start |

The label comes off at the end of every run, except in one case: when the pull request's head
moved while `ai-fix` worked, Specster pushes nothing (the push is leased on the head it read at
the start, never forced), comments that the pull request moved, and keeps the label. Remove it
and add it again to run on the new head.

## Budget, time and cost

- The budget per pull request works as it does per issue: `budget.max_usd_per_issue` caps every
  run on the pull request added up, found from Specster's own earlier comments, and
  `budget.max_usd_per_build` caps one run. A spent budget is a comment, the label comes off, and
  the run ends `budget_exhausted`.
- `build.max_minutes` (default 100) bounds both phases. The `evidence` and `fix` jobs have a
  `timeout-minutes` of 120, so Specster stops, comments and says why before the job is killed.
  Keep `build.max_minutes` below the job's timeout.
- Cost estimates, not measured yet: the evidence planner is about $0.10 to $0.25, plus the
  capture, which uses no model. A fix costs about what a build of the same size costs (see
  [measured costs](build.md#measured-costs)), with the planner on top.

## Things to know

- A fork never runs, and neither does a draft or a closed pull request: Specster refuses with a
  short comment. See [Security model](security.md#pull-requests).
- Specster's config always comes from the default branch. A pull request cannot change its own
  budget, trust or label names.
- Pushes made with `GITHUB_TOKEN` do not trigger the pull request's CI (a GitHub rule). To have
  CI run on what `ai-fix` pushes, give Specster its
  [own bot](configuration.md#your-own-bot-identity), whose App token does trigger it.
- Closing a pull request, merged or not, removes its `pr-<N>` folder from `specster-evidence`.
