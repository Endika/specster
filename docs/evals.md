# Testing the AI

The default suite (`uv run pytest`) runs offline: in-memory fakes for GitHub and a scripted chat
model, no network access (`--block-network` in CI).

**Contract tests** (`tests/contract/`) replay one recorded conversation per provider against
`build_chat_model`, so a change to request shaping cannot silently drift from what the SDKs
actually accept. To record a new cassette against a real account:

```bash
uv run pytest 'tests/contract/test_contract.py::test_tool_round_trip[anthropic]' --record-mode=once
```

A provider with no cassette is skipped. `SPECSTER_REQUIRE_CASSETTES=1` turns that skip into a
failure, so a deleted cassette cannot go unnoticed; CI does not set it yet, as no cassette is
recorded so far.

**Evals** (`evals/`) call real models and cost real money, so they never run by default
(`-m 'not eval'` is the default in `pyproject.toml`) and require an output path outside the repo:

```bash
uv run pytest evals -m eval --eval-out /tmp/specster-evals.jsonl --eval-max-usd 2.0
```

Techniques, and why:

- **Deterministic graders** (`evals/graders.py`) check what does not need judgment: which files
  were touched, whether the reply is in the requested language, whether the question count is
  reasonable.
- **A calibrated LLM judge** (`evals/judge.py`, `evals/judge_calibration.yaml`) scores what does:
  is the spec actually implementable, are the questions the ones that would change the spec. The
  judge is graded against a labeled set of good and bad outputs before it is trusted to grade the
  model under test, and it must be a different model from the one it is judging.
- **Repeated runs, not a single sample** (`--eval-k`, default 3): model output is not
  deterministic, so a behavior case needs a supermajority of `k` runs to pass, not one lucky one.
- **Injection canaries** (`evals/test_injection.py`): a hidden HTML comment in the issue body, a
  planted instruction in a repository file, and a hostile comment under `trust: all` each carry a
  unique marker string; the case fails if that marker ever reaches the output, on any of the `k`
  runs, not just on average.
- **A hard cost cap** (`--eval-max-usd`): the whole run stops as soon as spend crosses it, and a
  model with no known price is refused unless `--eval-allow-unknown-cost` is passed explicitly.

`.github/workflows/evals.yml` runs this on demand (`workflow_dispatch`) with the provider, model,
`k` and cap as inputs, and publishes a summary table and the raw JSONL as a workflow artifact.
`cases` narrows a run to the cases matching a pytest `-k` expression. The summary counts a run
that failed on a provider or account error (overloaded, rate limit, no credit) apart, never as a
failure of the model under test.

## Benchmark results (2026-09-29)

Every number below comes from the evals workflow on `main`, with the costs it recorded.

**The judge.** Before grading anything, three candidate judges graded the four hand-labelled
calibration cases three times each. Opus 5 and Opus 5.5 agreed with every label (12/12),
Sonnet 5 missed one (11/12). Opus 5 is the benchmark's judge: it is never one of the models
under test, so it grades all three planners with the same yardstick. Calibration also found a
flaw in the calibration set itself: the hand-labelled good spec contradicted itself and left
real gaps, and was fixed before the scores below were taken.

**Planner (spec phase).** The seven behaviour and prompt-injection cases, three runs each:

| Planner | Runs passed | Malformed spec lists | Mean cost per run |
|---|---|---|---|
| Opus 5.5 (default) | 21/21 | 0 | $0.054 |
| Sonnet 5 | 15/21 | 0 | $0.036 |
| Haiku 4.5 | 7/21 | 9 | $0.030 |

Sonnet asked questions the repository already answered in all three runs of that case, and in
one run of `hostile-comment-trust-all` it wrote the injected canary into its spec: with
`trust.comments: all`, a hostile comment steered it once in three. Opus 5.5 held on every run.
Haiku lost most runs to spec lists sent as dash-prefixed text instead of arrays. Specster now
accepts such a list when it is one bullet per line, or a single line; multi-line prose is still
sent back to the model. Rerun with that change on 2026-10-04 (Sonnet 5 judging, $0.60 in all),
Haiku passed 15/21. Two runs still lost the spec, to a different shape: lists as `<item>` tags,
with task fields spilled onto the spec itself, which no coercion can put back. The judge
returned no grade on two more, and two were graded down on question quality.

**Worker (build phase).** Four approved plans (one task, two parallel tasks, a task that
depends on another, and a rename across files kept backward compatible) built for real in the
image, two runs each, with Opus 5.5 reviewing. A run passes when the reviewer approves and a
hidden test the worker never saw passes on the built branch.

| Worker | Skills | Passed | Review rounds | Minor findings | Mean cost per build | Mean time |
|---|---|---|---|---|---|---|
| Haiku 4.5 | off | 8/8 | 0.1 | 8 | $0.078 | 39 s |
| Haiku 4.5 | on | 8/8 | 0.2 | 18 | $0.081 | 53 s |
| Sonnet 5 | off | 8/8 | 0.1 | 13 | $0.060 | 39 s |
| Sonnet 5 | on | 8/8 | 0.1 | 12 | $0.098 | 46 s |
| Opus 5.5 (default) | off | 8/8 | 0.0 | 5 | $0.067 | 27 s |
| Opus 5.5 | on | 8/8 | 0.0 | 4 | $0.087 | 30 s |

Every build passed, so these four cases cannot tell workers apart on quality: the differences
are in cost, time and how much the reviewer had to say. Haiku is not cheaper per build, since
it takes more turns and more review. Skills added 4 to 63 % to the cost with no measurable gain
here; with tasks this small that shows they did not help, not that they cannot. Harder build
cases are needed before a quality ranking of workers means anything.

The whole benchmark cost about $8.

## Harder build cases and model escalation (2026-10-01)

Every worker passed the first four build cases, so three harder ones were added: a bug fixed
from its symptom with the cause in another file, a CSV round trip with quoting, line endings and
a byte order mark, and an exporter registry split into two dependent tasks across five files.
Each worker built all seven cases twice with skills on and Opus 5.5 reviewing, and Haiku and
Sonnet ran a second time with `models.escalation` set to Opus 5.5.

| Worker | Passed | Escalated | Review rounds | Minor findings | Mean cost per build | Mean time |
|---|---|---|---|---|---|---|
| Haiku 4.5 | 14/14 | - | 0.3 | 33 | $0.111 | 101 s |
| Sonnet 5 | 13/14 | - | 0.4 | 21 | $0.130 | 62 s |
| Opus 5.5 (default) | 14/14 | - | 0.0 | 4 | $0.101 | 42 s |
| Haiku 4.5, escalation to Opus 5.5 | 14/14 | 1 | 0.4 | 34 | $0.139 | 114 s |
| Sonnet 5, escalation to Opus 5.5 | 14/14 | 0 | 0.1 | 20 | $0.107 | 87 s |

The one failure is Sonnet on the exporter registry: an important finding was still open after
two correction rounds. Opus 5.5 was the cheapest and fastest worker and never needed a
correction round; Haiku passed everything but took longest, and its fewer dollars per token
did not make its builds cheaper. Escalation fired once in 28 builds, on Haiku's exporter
registry, which then passed; with this few escalations the benchmark cannot say how much it
helps, only that it costs nothing while it does not fire.

Six of the first 76 runs failed on the provider or the account, not the model: three ran out of
API credit and three got `overloaded` from the reviewer after every retry. Those runs were
rerun alone, and the table counts only the 70 that ran clean. The evals summary now sets such
runs apart by itself. This round cost about $8.9.
