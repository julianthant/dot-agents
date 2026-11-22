## Codex

- The root session uses GPT-6 Astra medium; the user handles root exhaustion. Route every subagent,
  including read-only research and review, at the chosen model and effort. New native GPT-6 Luna
  subagents use max for short tasks only; retire each after its result and create a fresh agent for
  every further short task. Use GPT-6.1 Sol medium for longer tasks and reuse its context for
  feedback and corrections. Never use a generic or model-unset agent that can inherit the root model.
- The `scout`, `mechanic` and `builder` agents in `~/.codex/agents/` carry these routes; their
  descriptions say when to use each.
- User-named providers or models limit eligible routes to those choices and any named fallbacks.
  On confirmed quota exhaustion or verified route unavailability, switch only within that scope; if
  none remains, checkpoint and report. Standard speed only; no paid overages.
