# <img src="assets/specster-mascot.png" alt="Specster mascot" width="144" align="center"> Specster

Haunts your issues until they're clear.

Specster is a GitHub Action that turns an issue into a spec, and an approved spec into a pull
request. Label an issue `ai-spec`: Specster reads the thread and your code, then either asks
what is missing or posts a spec with a task plan. Label it `ai-build`: worker models write each
task, a reviewer model checks the result, and you get a pull request. Every comment says what it
cost.

## Install

**1. Add the workflow** as `.github/workflows/specster.yml`:

```yaml
name: Specster

on:
  issues:
    types: [labeled]
  pull_request:
    types: [closed]
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

concurrency:
  group: specster-issue-${{ github.event.issue.number || github.event.pull_request.number || inputs.issue_number }}
  cancel-in-progress: false

jobs:
  spec:
    if: (github.event_name == 'workflow_dispatch' && inputs.phase != 'build') || github.event.label.name == 'ai-spec'
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v7
      - uses: Endika/specster@v0
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          issue_number: ${{ inputs.issue_number }}
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}

  build:
    if: (github.event_name == 'workflow_dispatch' && inputs.phase == 'build') || github.event.label.name == 'ai-build'
    runs-on: ubuntu-latest
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

  cleanup:
    if: github.event_name == 'pull_request' && startsWith(github.head_ref, 'specster/issue-')
    runs-on: ubuntu-latest
    timeout-minutes: 5
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v7
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
  worker:   {provider: anthropic, model: claude-sonnet-5}                  # writes the code
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

Every key, its default and what it does: [Configuration](docs/configuration.md#configuration).

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
| Worker (the code) | $0.034 | **$0.067** | $0.117 |
| Reviewer | $0.023 | $0.046 | **$0.088** |

Bold is the default. Prices are USD per million tokens (input / output / cache read / cache
write): Opus 5.5 $4 / $20 / $0.20 / $5, Sonnet 5 $2 / $10 / $0.20 / $2.50, Haiku 4.5 $1 / $5 /
$0.10 / $1.25.

**Quality, measured.** In the [benchmark](docs/evals.md#benchmark-results-2026-09-29), graded by
a calibrated judge and by hidden tests:

| Role | Haiku 4.5 | Sonnet 5 | Opus 5.5 |
|---|---|---|---|
| Planner: spec and injection cases passed | 7/21 | 15/21 | **21/21** |
| Worker: builds passed (reviewer + hidden test) | 8/8 | **8/8** | 8/8 |
| Worker: mean cost per build, reviewer included | $0.078 | **$0.060** | $0.067 |

Opus 5.5 is the only planner that passed every case, and the only one no injected comment
steered. Every worker passed every build case, so those cases rank workers on cost alone.

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

## Roadmap

- **0.4 - build phase (done).** `ai-build` turns an approved plan into a reviewed pull request.
- **Other languages (done).** mise installs the toolchains a repository declares, or
  `build.tools` names, so projects in other or mixed languages build: see
  [Toolchains](docs/build.md#toolchains-for-other-languages).
- **Cost and quality benchmark (done).** A calibrated judge and hidden build tests: see
  [Benchmark results](docs/evals.md#benchmark-results-2026-09-29).
- **Next - harder build cases and model escalation**, so workers are ranked on quality too, and a
  task or review that keeps failing moves to a stronger model.
