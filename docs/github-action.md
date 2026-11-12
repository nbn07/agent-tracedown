# Run your agent in CI with the GitHub Action

The repo ships a composite GitHub Action (`action.yml`) that turns Tracedown
into a CI gate for agent changes:

1. It installs Tracedown and starts a fresh server on `127.0.0.1:7070`.
2. It runs **your command** — a test suite, a scenario script, an eval — while
   your agent exports its runs to that server.
3. It collects the runs and posts a **PR comment**: one row per conversation
   with its result and, for failures, the root cause and the step it happened
   at.
4. It compares each conversation's outcome with a **baseline file** from the
   previous run. A conversation that passed before and fails now is a
   *regression*, and the build fails (configurable).

## Usage

Your agent's runs have to reach the server the action starts. Point the SDK
at it the way you would locally — for example
`HttpExporter("http://127.0.0.1:7070")`, or the `endpoint=` argument of your
framework integration — then:

```yaml
name: agent-checks
on: [pull_request]
permissions:
  contents: read
  pull-requests: write   # for the report comment
jobs:
  tracedown:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      # Persist the baseline between runs, or nothing can regress:
      - uses: actions/cache@v4
        with:
          path: tracedown-baseline.json
          key: tracedown-baseline-${{ github.ref }}
          restore-keys: tracedown-baseline-

      - uses: nbn07/agent-tracedown@v0.6.1
        with:
          command: python scripts/run_support_scenarios.py
```

The first run records every conversation in the baseline and cannot fail on
regressions — there is nothing to compare against yet. From the second run
on, a previously-passing conversation that starts failing turns the build
red and is called out at the top of the comment.

A conversation's result is *failure* if any of its runs in this job failed;
the root cause shown is the most recent failure's.

## Inputs

| Input | Default | What it does |
| --- | --- | --- |
| `command` | *(required)* | The command that runs your agent against the action's server. |
| `project` | *(all)* | Only collect runs from this Tracedown project. |
| `baseline` | `tracedown-baseline.json` | Where the per-conversation outcomes live. Persist this file between runs (cache or commit it). |
| `fail-on-regression` | `true` | Set to `false` to report regressions in the comment without failing the build. |
| `comment` | `true` | Post the report as a PR comment. Needs a `pull_request` event; outside one, the report still lands in the job summary. |
| `python-version` | `3.12` | Python used to run Tracedown. |
| `tracedown-version` | *(latest)* | Pin a specific `agent-tracedown-server` release. |

The action has one output, `regressions`: the comma-separated names of the
conversations that regressed, for follow-up steps to consume.

## The reporter on its own

The comment and the gate are a small stdlib-only module in the server
package, so you can run the same logic against any server — including a
long-lived one — or in a different CI system:

```console
$ python -m tracedown_server.github_action \
    --endpoint http://127.0.0.1:7070 \
    --baseline tracedown-baseline.json --no-comment
```

It prints the report, updates the baseline, and exits `1` on a regression
(`2` if the server cannot be read). `--no-fail-on-regression` and `--project`
mirror the action inputs.
