# Telemetry

Specster can push each run's metrics and trace to any OTLP/HTTP backend (Datadog, Grafana Cloud,
an OpenTelemetry Collector) so you can chart and alert on runs without reading the Actions log.
It is off unless you set an endpoint. There is no `config.yml` key; everything is standard
`OTEL_*` environment variables on the Specster step.

## Turn it on

Set `OTEL_EXPORTER_OTLP_ENDPOINT` to send both metrics and traces. To send only one of them, or
each to a different place, set `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` and/or
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` instead: a signal is on only when its own endpoint or the
shared one is set, so a metrics endpoint alone sends no traces, and the other way round. With none
of them set, or with `OTEL_SDK_DISABLED=true`, no provider is installed and nothing leaves the
runner. The export is OTLP over HTTP with
protobuf. Headers come from `OTEL_EXPORTER_OTLP_HEADERS`, and the SDK reads the other standard
`OTEL_*` variables too, such as `OTEL_RESOURCE_ATTRIBUTES`.

The resource is `service.name=specster`, `service.version=<Specster version>` and
`service.instance.id=<GITHUB_RUN_ID>-<GITHUB_RUN_ATTEMPT>`.

Step-level `env:` reaches the action's container, so put the variables on the Specster step and
keep credentials in secrets.

### Grafana Cloud

```yaml
- uses: Endika/specster@v0
  env:
    OTEL_EXPORTER_OTLP_ENDPOINT: https://otlp-gateway-<zone>.grafana.net/otlp
    OTEL_EXPORTER_OTLP_HEADERS: Authorization=Basic ${{ secrets.GRAFANA_OTLP_AUTH }}
```

Copy the endpoint from your stack's OpenTelemetry tile: the host depends on when the region was
created. `GRAFANA_OTLP_AUTH` is the credential value that tile gives you for the `Basic`
Authorization header. The SDK appends `/v1/metrics` and `/v1/traces` to the endpoint.

### Datadog

Through a Datadog Agent that has OTLP/HTTP enabled on port 4318 (`otlp_config.receiver.protocols.http.endpoint: 0.0.0.0:4318` in `datadog.yaml`, or
`DD_OTLP_CONFIG_RECEIVER_PROTOCOLS_HTTP_ENDPOINT`). The runner must be able to reach the Agent,
so this suits self-hosted runners:

```yaml
- uses: Endika/specster@v0
  env:
    OTEL_EXPORTER_OTLP_ENDPOINT: http://datadog-agent.internal:4318
```

Or an OpenTelemetry Collector with a Datadog exporter, using the same variable to point at it.

Datadog also documents an OTLP intake that needs no Agent. It takes the API key in a
`dd-api-key` header, and its endpoint depends on your Datadog site, so copy the metrics and
traces URLs from Datadog's agentless OTLP pages:

```yaml
- uses: Endika/specster@v0
  env:
    OTEL_EXPORTER_OTLP_METRICS_ENDPOINT: <metrics intake URL for your site>
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: <traces intake URL for your site>
    OTEL_EXPORTER_OTLP_HEADERS: dd-api-key=${{ secrets.DD_API_KEY }}
```

Datadog's pages also describe `dd-otel-metric-config` (metric translation) and `compute_stats`
(trace metrics) headers; see them if you need those. Specster has not been run against the
intake, so check that metrics and traces arrive before you build alerts on them.

## What is sent

Everything is sent once, when the run ends.

### Metrics

All metrics are gauges with one value per run, so there is no temporality to choose. Every
metric carries `specster.repo`, `specster.phase` (`spec`, `build` or `cleanup`) and
`specster.outcome`. The issue number is never a metric attribute: each issue would be its own
billable series.

| Metric | Unit | Extra attributes |
|---|---|---|
| `specster.runs` | 1 | |
| `specster.run.cost` | USD | |
| `specster.run.duration` | s | |
| `specster.run.turns` | 1 | |
| `specster.run.tokens` | 1 | `specster.token.type`: `input`, `output`, `cache_read`, `cache_write` |
| `specster.run.truncations` | 1 | |
| `specster.run.warnings` | 1 | |
| `specster.role.cost` | USD | `specster.role`, `gen_ai.provider.name`, `gen_ai.request.model` |
| `specster.role.tokens` | 1 | the three above, plus `specster.token.type` |
| `specster.role.turns` | 1 | `specster.role`, `gen_ai.provider.name`, `gen_ai.request.model` |
| `specster.spec.files_read` | 1 | spec phase only |
| `specster.spec.comments` | 1 | `specster.kind`: `included`, `untrusted`, `after_label`, `edited_after_label`; spec phase only |
| `specster.spec.hidden_removed` | 1 | spec phase only |
| `specster.spec.skills` | 1 | `specster.kind`: `available`, `read`, `inlined`; spec phase only |
| `specster.build.tasks` | 1 | `specster.state`: `total`, `done`, `escalated`; build phase only |
| `specster.build.test_runs` | 1 | build phase only |
| `specster.build.parallel` | 1 | build phase only |
| `specster.build.review_rounds` | 1 | build phase only |
| `specster.build.evidence` | 1 | `specster.state`: `items`, `problems`; build phase only |

A cleanup run sends only `specster.runs`, with its outcome (`cleaned`, `skipped` or `error`).

### Trace

```
specster.run                 specster.repo, specster.phase, specster.issue, specster.outcome
├── chat <model>             gen_ai.operation.name, gen_ai.provider.name, gen_ai.request.model,
│                            gen_ai.usage.input_tokens, gen_ai.usage.output_tokens,
│                            specster.cache_read_tokens, specster.cache_write_tokens
├── tool <name>              specster.tool.error
└── (build only)
    ├── task <id>            specster.task.id, specster.task.round, specster.task.escalated,
    │                        specster.task.status
    ├── final_tests          specster.round (when there is one)
    ├── review               specster.round
    └── evidence
```

`chat` and `tool` spans hang under whichever span is running, so inside a build they sit under
their `task`, `review` or `evidence` span. Tasks that run in parallel all hang off the root
span. `specster.issue` is on the root span only.

A span that fails is marked ERROR, with the exception's class name in `error.type`. The root span
is also marked ERROR when the run's outcome is `error`.

## What is never sent

Not as a metric attribute, a span attribute or a span event:

- the issue number in metrics (it is only on the root span);
- the text of issues or comments;
- code or file contents;
- tool arguments;
- exception messages.

The sandbox that runs your tests never sees an `OTEL_*` variable: its environment is built from
scratch, and a test pins that.

## A failing backend does not fail the run

A down endpoint, a 4xx or a timeout never changes the run's outcome or exit code. Each export
attempt times out after 5 s, and the final flush is bounded: the traces get at most 60% of a
10 s budget, then the metrics are read and sent once with what is left. A signal that is off is
skipped. If the flush is cut short or fails, Specster prints a line to stderr and carries on.
Measured with a dead endpoint for both signals, the flush returns in about 9-10 s. Without this bound, the SDK's retries would hold the job open and bill runner
minutes after the work was done.

## Why gauges, and how to alert on them

Each run reports its own totals once, in a short-lived process, so a gauge fits: the value is
what that run cost. To get a daily or weekly total, sum the points over a window.

Datadog (metric `specster.run.cost`):

```
sum:specster.run.cost{*}.rollup(sum, 86400)
```

The `.rollup(<aggregator>, <seconds>)` form is from Datadog's rollup docs; check the whole
query in a metric monitor's editor before you rely on it for an alert on daily spend.

Grafana Cloud turns the dots into underscores and appends the unit to the name. Its documented
rule is to add `_<UNIT>` when the name does not already contain the unit, which for
`specster.run.cost` in `USD` should give `specster_run_cost_USD`:

```
sum(sum_over_time(specster_run_cost_USD[1d]))
```

How Mimir cases `USD` is not confirmed, so look the exact name up in the metrics browser
before you write the query or an alert rule. Both queries assume at least one point per
window; a day with no runs has no data rather than zero.
