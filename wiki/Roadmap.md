# Roadmap

What's done, what's being built, and what's next. Requests with a real failing run behind them get priority: share them in [Discussions](https://github.com/nbn07/agent-tracedown/discussions).

## Done

| Version | Highlights |
|---|---|
| 0.3.0 | First public release: Python and TypeScript SDKs, rule-based diagnosis with walk-back, who was at fault, claim checking, replay, web app, Langfuse and OpenTelemetry import |
| 0.4.0 | `tracedown up`: one command for server, web app and demo runs; "Diagnose a trace" (paste an export); "Where each claim came from"; prompt-injection detector; read-only mode; documentation site |
| 0.4.1 | Database moved out of cloud-synced folders (OneDrive, iCloud) |
| 0.4.2 | Tidier read-only [public demo](https://tracedown-demo.onrender.com/runs/) |
| 0.4.3 | "Top suspect" instead of confidence numbers, after measuring that scores don't predict correctness; Sonnet judge results published |
| 0.5.0 | Proven fixes: suggested fixes, tried by replaying the run and checked (below) |
| 0.6.0 | MCP server: `tracedown mcp` lets Claude Code, Cursor and other MCP clients ask why a run failed and what fix works |

## v0.5.0 "Proven fixes" (released)

Like a mechanic who swaps the part and takes the car for a test drive.

- [x] **Fix check:** a replay passes only if the blamed problem is gone, nothing new broke, the run finished, and your own test passed (`GET /api/v1/runs/{id}/fix-check`)
- [x] **`tdn.confirm_cause()`:** replay from the blamed model step with the output it should have given; three tries, all must pass
- [x] **No real side effects during checks:** tools are blocked unless you give a stand-in or name them as safe
- [x] **Suggested fixes** for common problems, and `tdn.try_fixes()` to try each one with the same check (prompt instruction for bad values and made-up facts, leaving out outdated or injected documents; loops and failing tools get advice only)
- [ ] LLM-worded suggestions when a judge is turned on
- [x] **In the web app:** a Fixes tab on each failed run (suggestions, whether each one worked, and the code to try them), and "Did this replay fix the run?" on every replay
- [ ] TypeScript SDK support (planned for v0.7)

Still to come: LLM-worded suggestions when a judge is on, and the TypeScript SDK (v0.7).

## Next

- **GitHub Action** that runs your agent's test conversations on every pull request
- **Write-back to Langfuse and Phoenix**, so the verdict appears in the tool you already use
- **Better accuracy on long multi-agent runs**, measured on Who&When

## How to influence it

Open a feature request with the [feature form](https://github.com/nbn07/agent-tracedown/issues/new?template=feature.yml), or vote with a 👍 on existing issues.
