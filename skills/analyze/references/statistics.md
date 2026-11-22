# Statistical judgment

Read for inference, experiments, hypothesis tests, forecasts and decisions about outliers.

Choose a method for the question and data-generating process. Distinguish descriptive summaries of
observed records from inference about an unseen population. Check sampling and assignment, repeated
observations, clustering, measurement scale and dependence before treating rows as independent samples.
Use summaries meaningful for the distribution and units; do not compute every statistic by habit.

For a comparison, identify the effect and baseline the user cares about. Consider selection, changing
composition, seasonality, concurrent interventions and missing outcomes. Random assignment or a
credible identification strategy is needed to support a causal interpretation; label exploratory
associations accordingly. Repeated peeking, many comparisons and post-hoc subgroup searches change
how evidence should be interpreted.

When inferential estimates are justified, compute effect sizes and uncertainty using an appropriate
method with stated assumptions. Distinguish statistical evidence from practical importance. A large
p-value does not establish equality, and a small one does not establish causality or business value.
Do not manufacture confidence intervals or significance from descriptive aggregates that lack the
needed information. Missing design or sample information may limit the result to description.

Treat unusual observations as investigation candidates. Verify measurement errors, units and plausible
rare events before excluding values. Retain the original analysis and show sensitivity to defensible
exclusions when that choice matters. Never delete inconvenient points to improve a narrative.

Forecast only when requested or needed for the decision. Compare with a sensible baseline, preserve
time order in validation, avoid future information leakage and consider coverage/seasonality. State
horizon and limitations; prediction intervals need a valid method. If history is inadequate, present
explicitly assumed scenarios rather than claiming a fitted forecast.

Original local adaptation of
[statistical-analysis](https://github.com/anthropics/knowledge-work-plugins/blob/main/data/skills/statistical-analysis/SKILL.md),
inspected 2026-09-05. Adopted assumption checks, effect/uncertainty reporting and careful causal claims;
replaced universal summary rules with method selection appropriate to the question and data.
