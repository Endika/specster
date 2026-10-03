# Security model

- **Who can trigger a run:** adding the `ai-spec` label needs GitHub triage permission; there is no
  other trigger.
- **Snapshot at the label:** with `trust.snapshot_at_label` (default on), only comments that
  existed before the labeling event are read; later or edited-since ones are excluded and counted
  in the metrics, not silently dropped.
- **Trust filter:** `trust.comments` (`all` | `collaborators` | `owner`) checks each comment's
  `author_association`. The issue author's own body is always read; excluded commenters are named
  in the comment. With `trust.issue_author` (default on), the issue author's comments are trusted
  like the body, so an outside reporter on a public repository can answer Specster's questions;
  the snapshot still drops the ones posted or edited after the label. Turn it off to hold the
  author to `trust.comments` like everyone else.
- **Hidden content shown, not just removed:** HTML comments and invisible/bidi characters are
  stripped from the issue body and comments before the model sees them, and shown verbatim (with a
  cap, and the cut reported) in a collapsed section of the reply.
- **Edited-body refusal:** if the issue body was edited (GitHub's `lastEditedAt`) after the
  triggering label, the run refuses with an error asking for the label to be re-added.
- **Structural nonce:** the thread handed to the model is wrapped in tags suffixed with a random
  per-run token (e.g. `<entry-3f9a...>`), and the model is told only tags with that suffix are
  structure. Text from the issue cannot forge a closing tag to escape its own quoting.
- **Identity pinning:** only comments by Specster's own login count as Specster's (earlier runs,
  the previous spec, the approved plan). That login is `identity.bot_login`, or else whatever the
  token reports about itself (over GraphQL). If neither gives an answer, the spec phase falls back
  to "any bot comment with a Specster marker" and says so in a warning, and the build phase refuses
  outright - with no model call and no branch touched - since its own spec could then have been
  posted by any bot, not just Specster. `github-actions[bot]` is the login of every workflow in the
  repository, so any of them could post a spec as Specster even with a resolved login; a build
  running as that login carries a warning rather than refusing. If `identity.bot_login` is set but
  differs from the login the token itself resolves to, a build also carries a warning (Specster
  will not recognise its own past comments) and still uses the configured one. Use a GitHub App
  ([Your own bot identity](configuration.md#your-own-bot-identity)) for builds.
- **Marker trust:** of a Specster comment's metrics markers, only the last one is read; a forged
  earlier one is ignored.
- **Skill confinement:** a skill loaded by `path` must resolve inside the checked-out repository,
  with no symlink and no `..` escape, or the run fails.
- **Pinned remote skills:** a `url` skill with `sha256` set fails the run if the fetched content's
  hash does not match; without one, it is loaded with a warning.
- **Scoped token:** `skills_auth_token` is only attached to requests to `raw.githubusercontent.com`,
  `github.com` or `api.github.com`, and is dropped by the HTTP client on any redirect to a
  different host.
- **Bot-sender guard:** any event whose sender is a GitHub App or bot is skipped outright. An
  installation token cannot reliably learn its own login (there is no `GET /user` for it), so this
  guard is broader than "ignore myself": it assumes a human always applies the label.

### Pull requests

`ai-evidence` and `ai-fix` ([Specster on any pull request](pull-requests.md)) read a pull request,
which anyone can write, so they have their own rules.

- **Forks never run.** The `evidence` and `fix` jobs start only for a branch of this repository,
  and Specster refuses a fork or a deleted fork again from the pull request itself. GitHub gives
  no secrets to a fork's `pull_request` run either, so there is nothing to start with.
- **Config and code come from the default branch.** Both jobs check out the default branch, not
  the pull request, so a pull request cannot raise its trust, its budget or its label names, and
  its code never runs with the job's token outside the sandbox. As a backstop, Python refuses a
  checkout that is not the tip of the default branch before it reads the pull request.
- **The workflow file is the exception.** On `pull_request`, GitHub runs the workflow file from
  the pull request's merge commit, so a same-repository pull request can edit the jobs
  themselves. That is acceptable because whoever pushes a branch to the repository already has
  write access, but it means the label gate in `if:` protects against other labels and forks, not
  against a collaborator who can write to the repository.
- **Everything from the pull request is untrusted:** title, description, diff, file paths and
  review comments are sanitized (hidden content is shown, not followed), wrapped in nonce-tagged
  blocks, and the prompts say to apply only what the change asks.
- **Only trusted, earlier review counts.** For `ai-fix`, a comment must come from someone
  `trust.comments` accepts, be created before the label and not be edited after it. Anyone else's
  comments are counted in the summary but never shown to a model.
- **Pushes are leased and never forced.** `ai-fix` pushes to the pull request's own branch with
  a lease on the head it read at the start. If anyone pushed meanwhile, nothing is pushed.
- **Never onto the default branch.** The label needs only triage, so `ai-fix` refuses, before
  any model call, a pull request whose head is the default branch, `specster-evidence` or a
  protected branch (a `main` to `release` pull request, say): its commits would land there
  without a review.
- **Answered markers are Specster's alone.** The marker that skips an item on a re-run sits on
  the last line of Specster's comment or reply, and quoted output (test logs, comments) is
  defanged so it cannot plant one.
- **Residual:** with `trust.comments: owner`, a collaborator with write access whom the setting
  excludes could edit Specster's own comment to append a marker and suppress items on a re-run.
  It can only hide items, never add one.
- **`GITHUB_TOKEN` pushes start no CI,** so what `ai-fix` pushes is not tested by the pull
  request's workflows unless Specster runs with an App token, which does trigger them.

### Limits

- **Pushed branches run push-triggered CI on unreviewed code.** A build pushes model-written code
  to `specster/issue-<n>`; with a GitHub App token that push starts every workflow triggered by
  `push` on any branch, with that workflow's secrets. Restrict push-triggered workflows that hold
  secrets to trusted branches (`on: push: branches: [main]`). CI triggered by `pull_request` on
  the pull request Specster opens runs that model-written code too, and a same-repository pull
  request gets the repository's secrets. A task cannot change
  `.github/workflows/**` or `.github/actions/**` unless `build.allow_workflow_changes: true`: the
  build refuses such a plan and the worker cannot write there.
- **Specster's own configuration is off limits too.** Once merged, a change to
  `.github/specster/**` (config and skills) or to the `config_path` file reconfigures every later
  run: its trust mode, budgets, test command and `allow_workflow_changes`. A task cannot change
  them unless `build.allow_config_changes: true`, a separate switch from the workflow one; the
  build refuses such a plan and the worker cannot write there.
- **The `specster-evidence` branch is written with the build's token.** Anyone or anything holding
  that token can rewrite the branch, so treat its files as the build's output, not as a record.
  The app's responses and server logs are published as they came back, unfiltered: never seed
  real data or secrets into the preview app. Its commits carry `[skip ci]` so `push` workflows do
  not run on them.
- **Repository files are not sanitized.** Only the issue body and comments go through the hidden-
  content filter; a file the model reads with `read_file` or `grep` is handed over as-is. The
  system prompt tells the model not to follow instructions found in repository files, and the
  `repo-file` injection eval exercises exactly this path, but there is no mechanical filter on repo
  content.
- **`trust.comments: all` on a public repository is risky:** anyone, with no association to the
  repo, can add context the model reads.
- **A GitHub App or bot comment containing a `<!-- specster:` marker is read as Specster's own,
  from any bot.** Do not install another workflow on the same issues that posts bot comments
  echoing user-controlled text with that marker; Specster cannot tell it apart from its own
  history.
- **Never use self-hosted runners on a public repository.** Any issue can trigger a run.
- **Private skills need `raw.githubusercontent.com` or `api.github.com` URLs.** A
  `github.com/<org>/<repo>/raw/...` URL redirects to `raw.githubusercontent.com`, a different host,
  so `skills_auth_token` is dropped on the way and the fetch fails.
- **A broken config only produces an issue comment if the event would have triggered a run under
  the default label names** (`ai-spec` etc.). If `labels.spec` was itself customized away from the
  default and that is the label that was just added, Specster cannot know that from a config it
  failed to parse, so it fails the job silently from the model's point of view - the error is only
  in the workflow log.
- **Filters from the main repository's own git config run as root while a build exports a
  tree.** Smudge filters set up in the checkout's `.git/config` (for example git-lfs with
  `lfs: true` in `actions/checkout`) are trusted and run during that export.
- **The Gemini free tier may use prompts to improve Google products.** Do not run it over company
  code unless you are on a tier where that is off.
