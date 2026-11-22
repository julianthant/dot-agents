# Skill improvement and evaluation

Use for `/skills eval` and substantive skill improvements. No model/API run or subagent dispatch
is implied by this reference; follow existing delegation and execution authorization.

## Compare the whole capability

Inspect the existing entrypoint, reachable references, scripts and evaluations before declaring a
gap. Compare overlapping upstream skills at the method level. Classify each contribution as already
covered, missing, better replacement, conditional, conflicting or unnecessary. Keep one owner.
Record the current behavior, source URL/revision or inspection date, adaptation, destination and
verification case in the requested report. Preserve licenses/attribution when copying source.
Do not install upstream hooks, runtimes, agents or dependencies as if they were documentation.

## Baseline and candidate

Before edits, retain hashes and a recoverable copy of the affected files. Select realistic scenarios
from the skill's `evals/evals.json` and the shared
[regression scenarios](../evals/evals.json). Include normal use, tempting misrouting, competing
instructions, prior authorization, partial failure and long-session state where relevant.

For actual comparative runs, give the evaluator only the user request, required artifacts and selected
skill snapshot. Keep expected answers out of the executing model's prompt. Use isolated workspaces
with the same fixtures, model/effort, tool access and task budget for baseline and candidate. New runs
require authorized execution; do not launch agents just because this procedure mentions evaluators.

Judge observable outcomes first: correct scope, correct artifact/answer, supported claims, permissions,
required progress and completion state. Apply deterministic artifact/trace checks before subjective
rubrics. Accept different valid execution paths; enforce order only when it is itself a requirement.
One model run is not a reliable success-rate estimate. Repeat uncertain cases when authorized and
report sample size. Human/self walkthroughs are useful inspection, not independent behavioral runs.

## Report without invented measurements

Record case ID, model/effort, baseline/candidate hashes, outcome, decisive trace/artifact evidence,
and measured input/output/cached tokens, tool calls and latency only when available. Missing telemetry
is unknown, not zero. Do not add cumulative usage snapshots together or equate file bytes to tokens.
Report correctness/permission regressions separately; average scores must not hide a critical failure.
Do not claim savings from a shorter prompt without actual comparable runs and successful outcomes.

If executions are unavailable, report structural checks and manual case walkthroughs as such and mark
behavioral runs NOT RUN. Keep fixtures pending. No fabricated traces or auto-approved assessments.

Provenance: original local adaptations of
[ECC skill-stocktake](https://github.com/affaan-m/ECC/tree/HEAD/skills/skill-stocktake),
[ECC skill-comply](https://github.com/affaan-m/ECC/tree/HEAD/skills/skill-comply),
[Superpowers writing-skills](https://github.com/obra/superpowers/blob/HEAD/skills/writing-skills/SKILL.md),
and [agent evaluation](https://github.com/muratcankoylan/Agent-Skills-for-Context-Engineering/blob/HEAD/skills/evaluation/SKILL.md),
reviewed 2026-09-05. No automatic graders, memory hooks or framework installation.

## Blind comparative runs

When authorized to compare a skill variant with real runs: hold the rubric (3–6 concrete criteria) back
from candidates; give each candidate a sanitized, project-shaped workspace and one organic user prompt
with no mention of evaluation, judging, rubrics or other candidates; never ask candidates to list the
skills they applied. A judge from a different model family scores outputs by sanitized label in one
pass. Grade whether the skill chain was followed from the files each candidate actually opened in its
own transcript, not from its claims; then read every output yourself and reconcile with the judge.
Adapted from pstack's eval playbook (MIT, `../LICENSE-pstack`).
