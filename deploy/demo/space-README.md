---
title: Tracedown
emoji: 🔎
colorFrom: red
colorTo: gray
sdk: docker
app_port: 7860
license: apache-2.0
short_description: Find the step that caused your AI agent's failure
---

# Tracedown live demo

A read-only [Tracedown](https://github.com/nbn07/agent-tracedown) with eight agents that fail in different ways: a stale document, a prompt injection, a payment failure reported as success, and more. Click any failed run to see the step that caused it, the evidence, and who was at fault.

To diagnose your own runs, install it locally:

```bash
pip install agent-tracedown-server && tracedown up
```
