# Build phase

How `ai-build` turns an approved spec into a pull request, how the tests are isolated, the
permissions it needs and how cost is capped.

**The approved plan.** A build only ever builds Specster's own last spec comment: the most recent
comment by `identity.bot_login` (or the token's own login) whose hidden metrics marker says
`phase: spec, outcome: spec`, carrying a `<!-- specster:plan [...] sha256=... -->` marker whose
hash still matches its own JSON. Everything else about "approved" is a build refusal (below).

**Refusals.** Adding `ai-build` is refused, with no model call and no branch touched, when:

- there is no such spec comment on the issue at all;
- the `ai-build` label was added before that spec comment was posted;
- Specster posted a newer spec-phase comment after it (a later `ai-spec` run's questions, error or
  spec) - answer it and add `ai-spec` again for a fresh or revised spec, then `ai-build`;
- the issue body was edited after the spec was posted;
- the spec comment itself was edited after Specster posted it;
- a trusted comment (by `trust.comments`/`trust.issue_author`'s rules) was posted after the spec
  and `build.allow_comments_after_spec` is `false` (the default) - fold it in with a fresh `ai-spec`
  first, or set `build.allow_comments_after_spec: true` to build anyway and leave it unapplied;
- a task id in the plan is longer than 40 characters, or a task's file path has a `.git` path
  component (the plan marker was tampered with or came from an old, incompatible planner);
- the checkout is not at the tip of the default branch the triggering event saw (a
  `workflow_dispatch` from another branch, or a stale checkout, would otherwise put that branch's
  own diff in the pull request).

Each refusal posts why and removes the `ai-build` label, without touching `spec-ready`, so the
issue is ready to try again once the cause is fixed.

**A red base.** With a `test_command`, the build first runs the tests on the base commit, in the
final-tests slot. If they already fail there, no worker could make them pass, so the build stops
before any model call and posts the output, `needs-human`. Set `build.allow_failing_base: true`
for an issue that is about fixing those tests.

**Errors.** A few problems only surface once Specster has already started acting, so they are
reported as an error comment ("Specster could not finish this run") rather than a refusal: the
branch `specster/issue-<n>` already exists on the remote (Specster never overwrites a branch -
delete it, or merge its pull request, then add `ai-build` again); the container did not start as
root (the Docker action always does; a local `uses: ./` or a manual `python -m specster` will
not); the checkout has no `.git` (`actions/checkout` missing before Specster); an invalid
provider or model in `models.worker`/`models.reviewer`; a sandbox failure mid-build (a test
process could not be safely torn down, so the whole build aborts rather than risk a compromised
sandbox reaching a later step); the branch could not be pushed; or GitHub refused to create the
pull request after a successful push (see [Permissions](#permissions)). An error always removes `ai-build`; it
also adds `needs-human`, but only for the two failures that happen mid-build - the sandbox abort
above, and a push that failed after the build had already produced commits - since those are the
cases with state worth a person looking at before retrying.

**Revision mode.** A trusted comment after the spec is exactly what the spec phase's revision mode
is for: add `ai-spec` again and Specster reads the previous spec comment (its text and its plan
JSON) as the baseline and posts a new spec with a "Changes from the previous spec" section instead
of starting over. `ai-build` on the new spec then sees no unapplied comments and proceeds.

**Levels and parallelism.** Tasks form a dependency graph from `depends_on`; Specster runs them
level by level (independent tasks first, then whatever depended only on those, and so on), and
within a level up to `build.max_parallel` tasks run at once, each in its own sandbox slot. Two
tasks in the same level are never allowed to touch the same file - the plan is refused earlier if
one is - so parallel workers can never race on a write.

**One commit per task.** A task that changes files gets exactly one commit (a task that leaves the
tree unchanged is not committed, just noted); the model proposes the commit subject, and an invalid
one (wrong conventional-commit shape, over 72 characters, or one that would close an issue on
merge, e.g. "fix: closes #3") falls back to `feat(<task-id>): <task title>` (or `fix(<task-id>):
address review findings` for a correction round).

**Correction rounds.** After every task finishes (and after the final test run below), a reviewer
model reads the integrated diff and either approves or returns findings. `critical` and `important`
findings block; `minor` ones never do. A blocked build gets up to `build.max_review_rounds`
(default 2) correction rounds: only the tasks named in a blocking finding run again, in the same
level order as the first pass (so two tasks that share a file across levels still never run at
once), adding a `fix(<task-id>): ...` commit on top. Findings not fixed within that many rounds end
the build as `not_approved`.

**The final test run.** Whatever `build.setup_command` and `build.test_command` say (a worker's own
`run_tests` calls, if any, are only advisory) is also run by Specster itself, once per round, on
the actually-integrated branch in a fresh sandboxed checkout - never on a single worker's own tree.
That result, not a worker's, is what the reviewer, the pull request and the comment show. Failing
final tests block the pull request even if the reviewer approves; if the reviewer names no task to
fix in that case, the build ends `not_approved` immediately rather than looping.

**The pull request.** Opened only once a round has no blocking findings and the final tests pass
(when `build.test_command` is set). Its base is the default branch, its head is
`specster/issue-<n>`; the body carries the spec's objective (or a link to the spec comment), a
table of every task with its status and commit(s), the test result, any minor findings, and -
with `build.close_issue` (default `true`) - a trailing `Closes #<issue>`. The push itself is
create-only (`--force-with-lease` for a ref that must not already exist): Specster can push a new
branch but never force-updates one, matching the existing-branch error above (see "Errors" above).

**Retrying.** A refusal only removes `ai-build`; nothing was ever pushed. A build that ends
`not_approved`, `failed` or budget-exhausted removes `ai-build` and adds `needs-human` - any
branch a task did manage to commit to stays pushed, so that work is not lost. An error (see
"Errors") always removes `ai-build` too, and adds `needs-human` only for the two mid-build
failure cases described there. Whichever it was, fix the cause and add `ai-build` again to retry;
if a branch was already pushed, delete it (or merge/close its pull request) first - Specster's
own existing-branch error is what stops a retry from reusing or overwriting it.

## Model escalation

With `models.escalation` set, a task its worker could not finish gets one more try with that
stronger model instead of failing the build:

```yaml
models:
  escalation: {provider: anthropic, model: claude-opus-5-5}
```

- A task that fails on its own (no `submit_task` within its turns, or tests still red) runs once
  more from the same base, in a fresh tree, with the escalation model. If that fails too, the
  task fails as before. A task stopped by the time limit, the budget or another worker's fatal
  error is never escalated.
- A task the reviewer blocks again after a correction round gets its next round with the
  escalation model.
- The retry is billed as its own role, `worker-escalated`, counts towards
  `budget.max_usd_per_build`, and only starts while the budget allows; otherwise the comment
  says it was not escalated and why. The task table marks the task "escalated to <model>".
- It is off by default, since it spends money nobody asked for.

## Before/after evidence

With `build.preview` set (see [every setting](../README.md#every-setting)) and a spec that
lists evidence requests, an approved build shows what the change does to the running app, not
only to the code. In the final-tests slot (slot 0) it runs `setup_command`, `seed_command` and
`serve_command` on the base commit, sends every request, stops that server, then does the same on
the branch head. The pull request gets a "Before and after" section: one row per request with its
status on each side, and each changed response's diff, collapsed. A side whose server never got
ready says why, with the tail of its log.

- It never fails the build. A server that does not start, a request that errors or a failed
  upload is reported in the pull request or the issue comment; the build's outcome and exit code
  stay what the build and review decided.
- The full responses, diffs and server logs go to the `specster-evidence` branch, an orphan branch
  with one folder per pull request (`pr-<N>/`), and the pull request links that folder. Its
  commits say `[skip ci]`, and a folder is removed when its pull request is closed. When the last
  folder goes, the branch is deleted too, so old evidence does not stay readable in its history.
- Limits: each request's JSON body stays under 2,000 characters, each response keeps its first
  64 KB, each diff its first 4,000 characters in the body (the whole diff is in the files), and a
  body that would pass GitHub's limit leaves out the last diffs and says so. The app is reached
  on loopback only, with no authentication. On a public repository, anything the seed puts in
  the app is published: seed fake data only.

### Screenshots

A spec can also list pages (a name, a path and why), up to 10 together with the requests. Each
page is screenshot on each side, after that side's requests, with its server still up: so the
pages show whatever state those requests left, the same on both sides. The pull request shows
one table per page, desktop and mobile by before and after, with the images linked at the
commit that published them on `specster-evidence`, and says whether the PNGs changed (byte for
byte; no pixel diff). A shot that could not be taken says why in its cell; a side whose
requests ran but whose shots did not (out of time, browser failure) only notes it in the page
table, not as a problem with the side. A page that answered with an HTTP error, or with no
response at all, says so under its shot ("the page answered 404"), so an error page does not
pass for the page that was asked for. The planner is told the preview's origin, its
`serve_command` and the path of its `ready_url`, and that paths are requested exactly as written:
a base path from the project's own config (Vite's `base`, for instance) only applies if
`serve_command` uses it.

- The browser is installed only when the approved spec has pages, once per build, as root:
  Playwright (pinned) and Chromium's headless shell with its system libraries, into
  `/opt/specster-browser`, never into the image. It takes about 30-60 s and downloads about
  400 MB; a failed or timed-out install (at most 300 s, and never past the build's time left)
  skips the screenshots with a warning and keeps the rest.
- It runs as the final-tests slot's unprivileged user, in that slot's sandbox, with only `PATH`,
  `HOME`, `TMPDIR`, the locale, the browsers' path and `build.test_env` in its environment:
  never a token, an API key or `OTEL_*`. Chromium starts with `--no-sandbox --disable-dev-shm-usage`, since the slot
  is the sandbox, and it is killed with the side's server.
- Each side's browser run is bounded by `build.test_timeout_s` and by the build's time left,
  and each page load by 30 s; a page that does not fit, or a side with no time left, gets a
  note instead of a screenshot.
- Every page is shot at 1280x800 (desktop) and 390x844 (mobile), full page, with locale
  `en-US`, time zone `UTC`, `reduced_motion` set, animations disabled and a fixed clock, after
  the network is idle and the fonts are loaded, so an unchanged page gives the same PNG.
- Limits: a page is cut at 6,000 pixels high and a PNG over 5 MB is dropped with a note; a body
  that would pass GitHub's limit leaves out the last pages and says so.
- The images are linked to the published commit, so GitHub may keep serving them after the PR's
  folder (or the branch) is removed; on a public repository treat the screenshots as published
  and seed fake data only.

## Toolchains for other languages

The image does not carry [mise](https://mise.jdx.dev): a build that needs toolchains
downloads it (pinned, and checked against its release's sha256 before it is unpacked), a few
seconds that only those builds pay, since each action run starts from a fresh container.
When a build has a `setup_command` or `test_command`, or collects evidence through `build.preview`, it installs the toolchains the repository declares at its root (`mise.toml`,
`.tool-versions`, `.nvmrc`, `.node-version`, `.ruby-version`, `.java-version`, `.go-version`,
`.bun-version`) plus anything `build.tools` names:

```yaml
build:
  tools: {node: "22", java: "21"}          # for a repository that declares nothing itself
  setup_command: [npm, ci]
  test_command: [npm, test]
```

What the repository declares wins over `build.tools` for the same tool. A repository mixing
languages lists several tools, and all of them end up on the commands' `PATH`. A repository that
declares nothing never downloads or runs mise, and a Python one keeps using the image's Python
and uv.

How it runs: once per build, before the base test run, mise installs as the final-tests slot's
unprivileged uid on an exported copy of the base commit, the same trust as `setup_command`, since
a `mise.toml` can run scripts. Only core tools install: asdf and vfox plugins are disabled, and
`build.tools` refuses backend-prefixed names such as `npm:x`. Root then takes the installed tree
over read-only, with no setuid or setgid bit left, so no slot can change what another one runs,
and every later command gets its `bin` directories first on `PATH`. A failed install stops the
build before any model call and shows mise's output.

## Test isolation

Specster (running as root, the way the Docker action starts) never runs anything from the
repository itself except `build.setup_command` and `build.test_command`, as argv lists taken
verbatim from the config - never a command chosen by the model. Each one runs as one of a fixed
set of unprivileged sandbox identities baked into the image: uid/gid `61000 + slot`, where slot 0
is reserved for the final integration test run and slots `1..max_parallel` are the workers, so
`getpwuid` resolves for every one of them.

Each worker's tree is not a `git worktree` of the main checkout: it is a fresh export of the
task's base commit (`git read-tree` + `checkout-index`, run by root) into a `tree` subdirectory of
a root-owned enclosure (mode `0750`, group-owned by the slot's gid so only that slot can enter
it), which then gets its own throwaway, credential-less git repository (`git init` + one commit)
before that tree - not the enclosure itself - is handed over: chowned and `chmod 0700` to the
slot's uid/gid. Root never runs git inside a worker's tree again after that hand-over.

Before any sandboxed command runs at all, Specster strips the checkout's git credentials: any
`http.*.extraheader`/`credential.*` entry and any `includeIf`-referenced file (where
`actions/checkout` v6+ keeps its token) are removed or locked to `0600`; `gha-creds-*.json`,
the file named by `GOOGLE_APPLICATION_CREDENTIALS`, and the `GITHUB_ENV`/`GITHUB_OUTPUT`/
`GITHUB_PATH`/`GITHUB_STEP_SUMMARY`/`GITHUB_STATE` file-command files (which a later workflow step
trusts) are all locked to `0600` too. The runner's mounted directories - `GITHUB_WORKSPACE`,
`/github/home` and `/github/runner_temp` - lose all access for other users (`chmod o-rwx`, never
through a symlink); they belong to the runner's user, so the runner's own later steps are
unaffected. The workspace stays that way after the build (`0750` from the usual `0755`), which
matters on a self-hosted runner where a later step runs as a uid that is neither its owner nor in
its group. A build refuses to start, before any model call, when `/var/run/docker.sock` is
readable or writable by other users (or owned by a sandbox uid or gid): through it a test could
drive Docker as root on the host. `Sandbox.run` refuses to start anything until this lockdown has
completed.

Each command also gets: a minimal environment (`PATH`, a private per-run `HOME`, `LANG=C.UTF-8`,
`TMPDIR` under that `HOME`, plus `build.test_env`) with no token in it; a wall-clock cutoff at
`build.test_timeout_s` (default 600 s; this repository's own config uses 900 s), after which the
whole process group is `SIGKILL`ed; combined stdout+stderr tailed to `build.test_output_max_kb`
(default 20 KB, with the cut reported, never silently dropped); and, applied to the command itself
right before it execs (`specster.sandbox_exec`), a per-process `RLIMIT_FSIZE` from
`build.test_output_max_file_mb` (default 1024 MB - a file that would cross it kills the process
with `SIGXFSZ`), an `RLIMIT_NPROC`, and `RLIMIT_CORE` set to 0. After the command ends (or times
out), Specster - as root - freezes (`SIGSTOP`) and kills (`SIGKILL`) every live process still owned
by that slot's uid, every round, until none are left, so a test cannot leave a daemon or a fork
running past its own turn.

An in-image check (`--isolation-check`, run in this repository's own CI, `docker` job) plants a
canary in the environment and in `.git/config`, runs as a sandbox slot, and asserts the canary is
not readable back from the parent's environment, from `/proc/1/environ`, or from `.git/config`,
nor from world-readable files in stand-ins for the runner's mounted directories; it also checks
that an open Docker socket is refused and a `0660` one cannot be connected to, and that three runs
each leaving 200 daemons behind never exhaust the slot's process limit.

**Limits:** the test process itself has network access. It never sees Specster's tokens or API
keys, the git credentials, the credential files or the runner's mounted directories above, but
whatever else in the container any user may read (the image's own files, the event payload in
`/github/workflow/event.json`) is in reach and could leave over the network; do not add secrets
to `build.test_env`, which the tests receive by design. A `setup_command` that fetches and runs
arbitrary code still runs as that code chooses. `/tmp` and `/dev/shm` are shared between sandbox
slots (only `TMPDIR`, under each command's own `HOME`, is private to that run, and
`build.test_env` cannot point it back at `/tmp`), so do not write secrets there. Isolating them
would take a mount namespace, which the action's unprivileged container cannot create. Python,
`uv` and git are baked into the image; any other toolchain comes from mise, downloaded on demand
(see "Toolchains for other languages" above) or from `build.setup_command`, unprivileged, with no
`apt`/`sudo` available. The reap loop kills a
process in D sleep without waiting for it to stop: CI checks it with a `vfork` parent, which waits
in D until its child is gone. A process wedged in D for good (a hung network filesystem) cannot be
staged without privileges; if one outlives the kill, the build stops with an error.

## Permissions

The build job needs `contents: write` (to push `specster/issue-<n>`), `pull-requests: write` (to
open the pull request) and `issues: write` (to comment, change labels, and close the issue on
merge with `build.close_issue`) - `.github/workflows/specster.yml`'s `build` job sets exactly
these three. With the default `GITHUB_TOKEN`, opening a pull request from a workflow run also
needs the repository setting Settings > Actions > General > "Allow GitHub Actions to create and
approve pull requests" turned on; without it, the branch is pushed but creating the pull request
is refused (HTTP 403), and a pull request opened with `GITHUB_TOKEN` never triggers its own
`pull_request` CI run regardless of that setting (a GitHub anti-recursion rule, not a Specster
one). A GitHub App installation token sidesteps both: give the App Contents (read and write) and
Pull requests (read and write) in addition to Issues (read and write) (see "Your own bot
identity"), and its pull requests do trigger CI normally.

## Metrics and budget

Every Specster comment ends with a collapsed section. A spec-phase comment carries one row, for
the planner:

| Provider | Model | Input | Cache read | Cache write | Output | Turns |
|---|---|---|---|---|---|---|

A build-phase comment carries one row per role instead (`worker`, `reviewer`), plus a cost column:

| Role | Provider | Model | Input | Cache read | Cache write | Output | Turns | Cost |
|---|---|---|---|---|---|---|---|---|

followed by files read, comments read/untrusted/after-the-label/edited-after-the-label, hidden
content removed, skills available and read, plan parallelism (for a spec) or build facts - tasks
done, test runs, workers used at once, review rounds (for a build) - anything truncated, and any
warnings. The same numbers are also written as JSON in a hidden marker,
`<!-- specster:metrics {...} -->`; a spec comment also carries the approved plan as
`<!-- specster:plan [...] sha256=... -->` so a build can build exactly what was approved. Only the
last metrics marker in a Specster comment is read back on the next run: earlier markers left over
from an edit are ignored.

`budget.max_usd_per_issue` sums the cost of every previous Specster run recorded on the issue -
spec and build runs alike - including ones that ended in an error or in `budget_exhausted` - a run
that failed after spending tokens still counts. When the sum reaches the cap, Specster posts that
the budget is spent and does not call the model (a build does not even start). A build also has
its own `budget.max_usd_per_build`, checked against that build's own spend only, and stopped
partway through if it is reached (whatever was committed so far is still pushed). Both caps are
checked before every model turn of the build - workers, escalated workers and the reviewer -
counting what the loops still running have spent so far, so a build overshoots a cap by at most
one turn per loop in flight. A task cut by a cap fails with the cap's reason, is never escalated,
and the build ends `budget_exhausted`. A run or a role
with unknown cost (no price for that model, and none set in `pricing:`) is never treated as $0: it
is called out in a warning and left out of both sums, so an unpriced model cannot silently
exhaust, or silently dodge, either cap.

## Measured costs

Spec-phase runs on this repository's own issues (`claude-opus-5-5`, effort medium) have cost
$0.09 to $0.26 for a full or revised spec and $0.08 to $0.09 per round of clarifying questions
(issues #20, #29, #34). Build-phase runs from the dogfood `build` job (worker `claude-sonnet-5`,
reviewer `claude-opus-5-5`), failures included:

| Issue | Tasks | Outcome | Worker $ | Reviewer $ | Total $ | Duration |
|---|---|---|---|---|---|---|
| #20 | 2 (one of 15 files) | failed: no submission after 40 turns | 0.865 | - | 0.865 | 3 min |
| #20 | 2 (one of 15 files) | failed: no submission after 80 turns | 2.140 | - | 2.140 | 7 min |
| #29 | 1 | failed: a zombie orphan failed a sandbox test (fixed in 0.4.2) | 1.340 | - | 1.340 | 25 min |
| #29 | 1 | error: the App token could not push | 0.064 | 0.070 | 0.133 | 2 min |
| #29 | 1 | pull request #32 opened, approved in the first review | 0.067 | 0.088 | 0.155 | 2.5 min |

A small, well-scoped task costs cents; a task packing many files burns its turns reading and can
fail without a commit, and cost grows faster than the turn count because every turn re-reads the
cached context.

