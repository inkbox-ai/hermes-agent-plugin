---
title: "Canary: contract tests failing against Hermes main"
labels: canary
---
The scheduled canary ran the host-interface contract tests against the latest
Hermes `main` and they **failed** — the upstream host interface has most likely
drifted (a renamed/removed `register_platform` kwarg, a moved symbol, or a
changed `MessageEvent`/`SendResult` shape).

Latest failing run: {{ env.RUN_URL }}

This issue auto-updates while the canary stays red, and can be closed once a run
goes green again.
