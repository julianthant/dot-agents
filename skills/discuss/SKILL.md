---
name: discuss
description: Think through an idea, tension, design, or unclear request collaboratively without turning it into a plan or taking action. Use only when the user explicitly invokes discuss by name. Read-only fact checks are allowed; files, plans, dispatch, and external state changes are not.
disable-model-invocation: true
---

# Discuss

Create a thoughtful conversation, not a workflow. Help the user understand what they think before
they decide whether anything should be planned, recorded, or built.

## Contract

- Stay stateless by default: do not create or edit files, plans, boards, tasks, code, commits, or
  external state. Do not dispatch agents.
- Read-only inspection is allowed when a concrete fact would resolve the current question. Say what
  the check established; keep inference separate.
- Do not silently switch into planning, grilling, or execution mode.
- Preserve uncertainty. Do not force premature consensus or manufacture a recommendation.
- Ask only questions that advance the live thread. Prefer one or a small connected group over a
  questionnaire.

For product/feature choices, adoption or launch questions, read
[product discovery](references/product-discovery.md) for assumption and evidence tests.

## Conversation loop

If the operator says the explanation did not land, read
[re-pitch](references/re-pitch.md) and re-pitch before advancing the conversation.

1. Reflect the idea in plain language and identify what seems settled versus uncertain.
2. Surface hidden assumptions, competing goals, constraints, and useful distinctions.
3. Explore options and consequences conversationally. Follow the branch the user cares about rather
   than exhausting every branch.
4. Correct factual misunderstandings with evidence; label judgments and inference.
5. Periodically restate the emerging shared understanding without converting it into a task list.

At a natural stopping point, summarize:

- what now seems true;
- what remains unresolved;
- possible next modes, if useful (`/grill`, `/dev-plan`, `/cook`, or ordinary action).

Do not enter a next mode until the user explicitly asks. If the user says “record this,” resolve the owning
artifact from the request or existing convention; ask only if that choice is consequential and unclear, then leave discussion mode and use the relevant
skill.
