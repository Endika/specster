# <img src="assets/specster-mascot.png" alt="Specster mascot" width="144" align="center"> Specster

Haunts your issues until they're clear.

Specster is a GitHub Action that turns an issue into a spec, and an approved spec into a pull
request. Label an issue `ai-spec`: Specster reads the thread and your code, then either asks
what is missing or posts a spec with a task plan. Label it `ai-build`: worker models write each
task, a reviewer model checks the result, and you get a pull request. Every comment says what it
cost.

## Try it

Two minutes, one file and one secret, no config. Specster only writes specs here; building
pull requests comes in [Full setup](#full-setup).

1. Save this as `.github/workflows/specster.yml`:

   ```yaml
   name: Specster
   on:
     issues:
       types: [labeled]
   permissions:
     contents: read
     issues: write
   jobs:
     spec:
       if: github.event.label.name == 'ai-spec'
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v7
         - uses: Endika/specster@v0
           with:
             github_token: ${{ secrets.GITHUB_TOKEN }}
             anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
   ```

2. Add your key as a repository secret named `ANTHROPIC_API_KEY` (Settings > Secrets and
   variables > Actions).
3. Open an issue and add the label `ai-spec`, creating it the first time. In about a minute
   Specster answers with questions or a spec, and the footer says what it cost.

## Full setup

Specs, builds, `ai-evidence` and `ai-fix` on pull requests, evidence cleanup and a manual trigger.

**1. Add the workflow** as `.github/workflows/specster.yml`, in place of the one above:

```yaml
name: Specster

on:
  issues:
    types: [labeled]
  pull_request:
    types: [labeled, closed]
  workflow_dispatch:
    inputs:
      issue_number:
        description: Issue to process
        required: true
      phase:
        description: Phase to run
        type: choice
        options: [spec, build]
        default: spec

permissions:
  contents: read
  issues: write

jobs:
  spec:
    if: (github.event_name == 'workflow_dispatch' && inputs.phase != 'build') || (github.event_name == 'issues' && github.event.label.name == 'ai-spec')
    runs-on: ubuntu-latest
    concurrency:
      group: specster-issue-${{ github.event.issue.number || github.event.pull_request.number || inputs.issue_number }}
      cancel-in-progress: false
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v7
      - uses: Endika/specster@v0
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          issue_number: ${{ inputs.issue_number }}
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}

  build:
    if: (github.event_name == 'workflow_dispatch' && inputs.phase == 'build') || (github.event_name == 'issues' && github.event.label.name == 'ai-build')
    runs-on: ubuntu-latest
    concurrency:
      group: specster-issue-${{ github.event.issue.number || github.event.pull_request.number || inputs.issue_number }}
      cancel-in-progress: false
    timeout-minutes: 120
    permissions:
      contents: write
      pull-requests: write
      issues: write
    steps:
      - uses: actions/checkout@v7
      - uses: Endika/specster@v0
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          issue_number: ${{ inputs.issue_number }}
          phase: ${{ inputs.phase || 'build' }}
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}

  evidence:
    if: github.event_name == 'pull_request' && github.event.action == 'labeled' && github.event.label.name == 'ai-evidence' && github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    concurrency:
      group: specster-issue-${{ github.event.issue.number || github.event.pull_request.number || inputs.issue_number }}
      cancel-in-progress: false
    timeout-minutes: 60
    permissions:
      contents: write
      pull-requests: write
    steps:
      - uses: actions/checkout@v7
        with:
          ref: ${{ github.event.repository.default_branch }}
      - uses: Endika/specster@v0
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}

  fix:
    if: github.event_name == 'pull_request' && github.event.action == 'labeled' && github.event.label.name == 'ai-fix' && github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    concurrency:
      group: specster-issue-${{ github.event.issue.number || github.event.pull_request.number || inputs.issue_number }}
      cancel-in-progress: false
    timeout-minutes: 120
    permissions:
      contents: write
      pull-requests: write
    steps:
      - uses: actions/checkout@v7
        with:
          ref: ${{ github.event.repository.default_branch }}
      - uses: Endika/specster@v0
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}

  cleanup:
    if: github.event_name == 'pull_request' && github.event.action == 'closed' && github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    concurrency:
      group: specster-issue-${{ github.event.issue.number || github.event.pull_request.number || inputs.issue_number }}
      cancel-in-progress: false
    timeout-minutes: 5
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v7
        with:
          ref: ${{ github.event.repository.default_branch }}
      - uses: Endika/specster@v0
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
```

**2. Add your model key** as a repository secret named `ANTHROPIC_API_KEY` (Settings > Secrets
and variables > Actions). Other providers work too: see [Providers](docs/configuration.md#providers).

**3. Let Actions open pull requests** (only for `ai-build`): Settings > Actions > General > "Allow
GitHub Actions to create and approve pull requests". Or give Specster its
[own bot](docs/configuration.md#your-own-bot-identity), which also makes the pull request's CI run.

That is all: no server, no database. Everything Specster knows lives in the issue and the repo.

## Use

| You add | Specster does | The issue ends with |
|---|---|---|
| `ai-spec` | reads the issue, its comments and your code | `needs-human` and questions, or `spec-ready` and a spec |
| an answer, then `ai-spec` again | writes the spec, or revises the last one with only what you asked | `spec-ready` |
| `ai-build` | builds the approved plan, one commit per task, tests in a sandbox, then a review | `ai-pr` and a pull request, or `needs-human` and why |

Adding a label needs the "triage" permission, so that is who can trigger Specster. The full
state diagram, with every refusal and error, is in [Labels and states](docs/configuration.md#labels-and-states).

## Configure

Nothing is required. To change the defaults, add `.github/specster/config.yml`; these are the
settings most repos touch:

```yaml
models:
  planner:  {provider: anthropic, model: claude-opus-5-5}                  # writes the spec
  worker:   {provider: anthropic, model: claude-opus-5-5}                  # writes the code
  reviewer: {provider: anthropic, model: claude-opus-5-5, effort: medium}  # reviews the build
  # escalation: {provider: anthropic, model: claude-opus-5-5}  # retry a failed task, off by default
budget:
  max_usd_per_issue: 8.0    # every run on the issue added up
  max_usd_per_build: 5.0
build:
  setup_command: [uv, sync, --locked]          # run once before the tests
  test_command: [uv, run, pytest, -q]          # without it, no test ever runs
  # preview: {serve_command: [uv, run, app], ready_url: "http://127.0.0.1:8000/"}  # PR evidence
persona:
  language: en              # questions, specs and pull requests in this language
trust:
  comments: collaborators   # whose comments count: all, collaborators or owner
```

### Every setting

<details>
<summary>All config keys and their defaults</summary>

Optional file at `.github/specster/config.yml` (path set by the `config_path` input). A missing
file or a missing key uses the default shown below. An unknown key or an invalid value fails the
run with a message naming the key. Action inputs (`github_token`, `config_path`, the `*_api_key`
inputs, `skills_auth_token`) always take precedence over this file.

```yaml
models:
  planner:                     # writes the spec and the task plan
    provider: anthropic        # anthropic | openai | openai-compatible | azure-openai | bedrock | vertex-anthropic | gemini | vertex-gemini
    model: claude-opus-5-5
    max_tokens: 16000
    effort: null                # low | medium | high | xhigh | max, provider-specific; null leaves the SDK default
    base_url: null               # required for openai-compatible and azure-openai; rejected for bedrock, vertex-anthropic, gemini, vertex-gemini
    region: null                 # required for bedrock, vertex-anthropic, vertex-gemini
    project: null                # required for vertex-anthropic, vertex-gemini
    api_version: null            # required for azure-openai
    api_key_env: null            # overrides which environment variable carries the API key
    token_param: null            # max_tokens | max_completion_tokens; provider default otherwise
    max_retries: 4
  worker:                       # writes one task's code in the build phase; same fields as planner
    provider: anthropic
    model: claude-opus-5-5
  reviewer:                     # reviews the build's diff before the pull request; same fields as planner
    provider: anthropic
    model: claude-opus-5-5
    effort: medium
  escalation: null              # a stronger worker for one more try at a failed or twice-blocked task;
                                # e.g. {provider: anthropic, model: claude-opus-5-5}; see docs/build.md

labels:
  spec: ai-spec                 # trigger label for the spec phase
  needs_human: needs-human      # applied when Specster asks questions, or a build needs a human
  ready: spec-ready             # applied when Specster publishes a spec
  build: ai-build                # trigger label for the build phase
  built: ai-pr                    # applied when a build opens a pull request
  evidence: ai-evidence          # trigger label on a pull request: before/after evidence
  fix: ai-fix                    # trigger label on a pull request: apply its open review threads

trust:
  comments: collaborators       # all | collaborators | owner: who counts, by author_association
  issue_author: true             # the issue author's comments count too, whatever their association
  snapshot_at_label: true        # only count comments that existed before the triggering label event

identity:
  bot_login: null                # the login Specster comments as, e.g. specster-endika[bot];
                                  # null asks the token, which an App installation token may not
                                  # answer. A build refuses outright when neither gives an answer.

skills:
  autodiscover: true             # pick up AGENTS.md, CLAUDE.md, .github/copilot-instructions.md,
                                  # .cursorrules, .cursor/rules/*.mdc, CONTRIBUTING.md,
                                  # .github/specster/skills/*.md
  sources: []                    # extra skills, e.g.:
                                  #   - path: docs/spec-style.md
                                  #   - url: https://raw.githubusercontent.com/org/repo/main/skill.md
                                  #     sha256: "<64 hex chars>"
                                  #     phases: [spec]        # spec | build | review; default: all three
  load: always                    # always: every skill of the phase is loaded (up to max_tokens)
                                  # model_decides: the model sees names only and may read none
  max_tokens: 20000                # token budget for skills inlined when load: always

repo_map:
  max_tokens: 8000
  exclude: ["node_modules/", "dist/", "build/", "vendor/", "*.lock", "*.min.js"]

persona:
  name: Specster
  avatar_url: https://raw.githubusercontent.com/Endika/specster/main/assets/specster-avatar.png
  header: true
  humor: light                    # off | light | spooky; only ever shows up in closing_line
  closing_line: generated          # off | generated | fixed
  closing_text: ""                  # required when closing_line: fixed
  language: en                      # language of questions, spec and build text
  style: concise                    # concise | detailed: how much text the comments carry
  max_questions: 3                  # 1-5 questions per round

budget:
  max_usd_per_issue: 8.0             # null removes the cap; sums every run on the issue, spec and build alike
  max_usd_per_build: 5.0              # null removes the cap; this one build's own spend
  max_turns: 30                       # hard stop on the agent loop (any role), independent of cost

build:
  max_parallel: 2                     # workers running at once, 1-8; also the sandbox slots created
  max_turns_per_task: 40              # hard stop on one worker's agent loop
  max_review_rounds: 2                 # correction rounds after a blocked review before giving up
  setup_command: null                  # argv list run once before test_command, as the sandbox uid; null skips it
  test_command: null                   # argv list run as the sandbox uid; null means no tests ever run
  test_env: {}                         # extra environment variables for setup_command and test_command (not HOME or TMPDIR)
  tools: {}                            # toolchains mise installs, e.g. {node: "22", java: "21"}; see docs/build.md
  test_timeout_s: 600                  # wall-clock limit per invocation of either command
  test_output_max_kb: 20               # tail kept of each command's combined stdout+stderr
  test_output_max_file_mb: 1024        # RLIMIT_FSIZE per process; a file over this kills the command
  max_minutes: 100                     # wall-clock limit on the whole build; keep it below the job's timeout-minutes
  close_issue: true                    # the pull request body carries "Closes #<issue>"
  allow_comments_after_spec: false     # true lets a build run despite trusted comments posted after the spec
  allow_failing_base: false            # true builds even when test_command already fails before any task
  allow_workflow_changes: false        # true lets a task change .github/workflows/** and .github/actions/**
  allow_config_changes: false          # true lets a task change .github/specster/** and the config_path file
  # Before/after evidence for the pull request (off unless set): Specster starts the app at
  # the base commit and at the branch head, sends both the requests the spec lists and
  # screenshots its pages.
  preview:
    serve_command: [python, -m, app]           # runs in the sandbox; must keep running
    ready_url: http://127.0.0.1:8000/health    # polled until it answers; loopback only
    seed_command: [python, seed.py]            # optional, before serve_command; no real data
    ready_timeout_s: 60                        # at most 300

pricing: {}                            # override or add prices, USD per million tokens:
                                        #   claude-haiku-4-5: {input: 1.0, output: 5.0, cache_read: 0.1, cache_write: 1.25}
```

`skills.sources[].phases` defaults to `all` (spec, build and review); with `load: always` every
skill scoped to the phase that is running is inlined into the prompt, up to `skills.max_tokens`.
`budget.max_usd_per_build` and `budget.max_usd_per_issue` (5.0 and 8.0 by default) are safety
caps chosen before any build's real cost was measured, not expected spend - see [what it costs](#what-it-costs).

</details>

<details>
<summary>Action inputs and outputs</summary>

Everything `Endika/specster@v0` accepts. Pass the key(s) of whichever provider(s) `models.planner`,
`models.worker` and `models.reviewer` are set to (they can differ); unused key inputs stay empty.

| Input | Required | Default | What it is |
|---|---|---|---|
| `github_token` | yes | | Reads the issue and comments; a build also pushes and opens a pull request. `GITHUB_TOKEN` comments as github-actions[bot]; a GitHub App token comments as your bot. |
| `config_path` | no | `.github/specster/config.yml` | Path of the config file in the repository. |
| `issue_number` | no | | Issue to process when the workflow runs from `workflow_dispatch`. |
| `phase` | no | `spec` | Phase to run when triggered by `workflow_dispatch`: `spec` or `build`. Ignored for the `issues: labeled` event, where the label itself decides the phase. |
| `anthropic_api_key` | no | | Key for provider `anthropic`. |
| `openai_api_key` | no | | Key for provider `openai`. |
| `gemini_api_key` | no | | Key for provider `gemini` (and for `openai-compatible` pointed at Gemini with `api_key_env: GEMINI_API_KEY`). |
| `azure_openai_api_key` | no | | Key for provider `azure-openai`. |
| `skills_auth_token` | no | | Token for skills stored in private GitHub repositories; only sent to GitHub hosts. |

| Output | Values |
|---|---|
| `outcome` | `questions`, `spec`, `refused`, `pr_opened`, `not_approved`, `build_failed`, `error`, `budget_exhausted`, `cleaned` (a closed pull request's evidence removed), or `skipped` when the event was not for Specster |

Providers that take no key input, and how each one logs in: [Providers](docs/configuration.md#providers).

</details>

## What it costs

Each comment has a footer with its tokens and cost per model, and the budget caps stop a run
before it spends past them. Measured on this repository:

| Run | Models | Cost | Time |
|---|---|---|---|
| Clarifying questions | Opus 5.5 | $0.08 - $0.09 | ~20 s |
| A spec, or a revised one | Opus 5.5 | $0.09 - $0.26 | ~30 s |
| A one-task build, pull request opened | Sonnet 5 worker, Opus 5.5 reviewer | $0.13 - $0.16 | 2 - 3 min |
| A 15-file task that did not finish | Sonnet 5 worker | $0.87 - $2.14 | 3 - 7 min |

A task over five files is flagged in the spec, since its worker has to read and edit all of
them within one task's turns. All measured runs, failures included, are in
[Build phase](docs/build.md#measured-costs).

**Same runs, other models.** The tokens of the #29 spec and build recomputed at each model's
list price:

| Role | Haiku 4.5 | Sonnet 5 | Opus 5.5 |
|---|---|---|---|
| Planner (the spec) | $0.037 | $0.074 | **$0.139** |
| Worker (the code) | $0.034 | $0.067 | **$0.117** |
| Reviewer | $0.023 | $0.046 | **$0.088** |

Bold is the default. Prices are USD per million tokens (input / output / cache read / cache
write): Opus 5.5 $4 / $20 / $0.20 / $5, Sonnet 5 $2 / $10 / $0.20 / $2.50, Haiku 4.5 $1 / $5 /
$0.10 / $1.25.

**Quality, measured.** In the [benchmark](docs/evals.md#benchmark-results-2026-09-29), graded by
a calibrated judge and by hidden tests:

| Role | Haiku 4.5 | Sonnet 5 | Opus 5.5 |
|---|---|---|---|
| Planner: spec and injection cases passed | 7/21 | 15/21 | **21/21** |
| Worker: [builds passed](docs/evals.md#harder-build-cases-and-model-escalation-2026-10-01) (reviewer + hidden test) | 14/14 | 13/14 | **14/14** |
| Worker: mean cost per build, reviewer included | $0.111 | $0.130 | **$0.101** |

Opus 5.5 is the only planner that passed every case, and the only one no injected comment
steered. As a worker it was also the cheapest and fastest on the seven build cases, and the only
one the reviewer never sent back, so it is the default worker too.

## Safety in short

- The issue thread is treated as untrusted data; hidden content is stripped before any model sees it.
- Model-written code runs only as an unprivileged sandbox user, without Specster's tokens or keys.
- A build never touches workflows or Specster's own config unless you allow it.
- Specster never overwrites a branch and never merges: every pull request waits for a person.
- Budget, turn and time limits cap every run.

The details and the known limits: [Security model](docs/security.md).

## More

- [Configuration reference](docs/configuration.md): the workflow line by line, inputs, every
  config key, providers, cloud logins, your own bot.
- [Build phase](docs/build.md): how a build runs, test isolation, permissions, metrics and budget.
- [Security model](docs/security.md): what Specster defends against and what it cannot.
- [Testing the AI](docs/evals.md): the evals and the contract cassettes.
- [Telemetry](docs/telemetry.md): send each run's metrics and trace to Datadog or Grafana.

## Roadmap

- **0.4 - build phase (done).** `ai-build` turns an approved plan into a reviewed pull request.
- **Other languages (done).** mise installs the toolchains a repository declares, or
  `build.tools` names, so projects in other or mixed languages build: see
  [Toolchains](docs/build.md#toolchains-for-other-languages).
- **Cost and quality benchmark (done).** A calibrated judge and hidden build tests: see
  [Benchmark results](docs/evals.md#benchmark-results-2026-09-29).
- **Before/after evidence, part 1 (done).** With `build.preview` set, the pull request shows how
  the app's HTTP and JSON responses change: see
  [Before/after evidence](docs/build.md#beforeafter-evidence).
- **Before/after evidence, part 2: screenshots (done).** Pages the spec lists are screenshot on
  desktop and mobile at the base and at the head, side by side in the pull request: see
  [Screenshots](docs/build.md#screenshots). Evidence on any pull request rather than only
  Specster's is still to come.
- **Harder build cases and model escalation (done).** A task or review that keeps failing can
  move to a stronger model: see [Model escalation](docs/build.md#model-escalation) and the
  [results](docs/evals.md#harder-build-cases-and-model-escalation-2026-10-01).
