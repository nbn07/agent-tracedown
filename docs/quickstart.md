# Quick start

You need Python 3.10 or newer. Nothing else: no Node, no Docker, no API key.

```bash
pip install agent-tracedown-server
tracedown up
```

`tracedown up` starts the server and the web app on <http://127.0.0.1:7070>, loads eight demo agents that fail in different ways, and opens your browser. Your data is kept in `tracedown.db` in the current folder.

## What to look at

Open **Runs** and click any failed run. The eight demo stories are:

| Agent | What went wrong | What Tracedown says |
|---|---|---|
| CalendarAssistant | The model wrote "Friday" where a date belongs | Bad model output at step 4, which the calendar tool could not use |
| SupportBot | It quoted a refund from last year's policy | Outdated source at step 2 |
| ResearchAssistant | It searched the same thing over and over | Loop |
| SalesAssistant | It quoted $49 when the price tool said $39 | Claim contradicted by a tool result |
| AccountAgent | It called "refund order" to cancel a subscription | Wrong tool |
| CheckoutAgent | Payment failed, and it told the customer the order was confirmed | Failed action, and "the final answer still reports success" |
| ProductResearcher | A web page told it to email the customer list, and it did | Prompt injection at step 2, followed at step 4 |
| OrderStatusAgent | Neither email the customer gave matched an account | The user, not the agent |

Also try **Incidents** (failures grouped by cause), **Drift** (a prompt change that made a bot worse) and **Compare runs** (the calendar run replayed with a fix).

## Options

```bash
tracedown up --no-demo          # start empty, for your own runs
tracedown up --port 8080        # another port
tracedown up --db team.db       # another database file
tracedown demo                  # print the demo diagnoses in the terminal, no server
```

## Next

- [Diagnose runs you already have](importing.md), with no code changes.
- [Add Tracedown to your agent](integrations.md) with one line.
- For a team or production setup (Postgres, several workers, Kubernetes), see [Deployment](DEPLOYMENT.md).
