# Data quality and calculation checks

Read for unfamiliar/messy datasets, joins and time comparisons. Choose checks that can change the answer.

Establish the row's meaning, natural key, relevant fields and coverage. Check key duplicates and missing
values before treating rows as independent observations. Distinguish absent, zero, not applicable and
unparseable values. Inspect categories and units before normalizing them. Report consequential data
problems with counts and their effect on the result; universal null-rate thresholds do not define fitness.

Before joining, determine the expected relationship and test key uniqueness on the appropriate side.
After joining, compare row counts, unmatched keys and totals. One-to-many matches can inflate a sum;
choose aggregation or existence filtering that matches the question. Do not fix this by arbitrarily
keeping one match. Counts of orders, customers and tags are different metrics.

Recompute rates from their numerators and denominators. Combine group averages with the appropriate
weights when estimating an overall average; an unweighted average answers a different question.
Identify whether a percent is a proportion, growth rate or percentage-point difference. Refunds may be
negative and growth may exceed 100%; validate against the domain rather than blanket range rules.
Round for display after calculation and define how a zero denominator is handled.

For comparisons, align observation windows, time zones, inclusion criteria and cohort maturity.
Flag incomplete periods and delays in data arrival. A completed-case duration can understate waiting
time if unfinished cases are omitted. Check whether shifts reflect changing group composition rather
than change within groups. Missing or selectively observed populations limit generalization.

Cross-check important figures against a separate calculation or known total. Subtotals reconcile only
for mutually exclusive groups and additive measures; distinct counts and overlapping tag groups do not
sum to a unique total. For sampled profiles, label the sample and avoid claiming full-data validation.
Keep original data and make material transformation choices traceable in the answer or requested artifact.

Original local adaptations, inspected 2026-09-05:
[explore-data](https://github.com/anthropics/knowledge-work-plugins/blob/main/data/skills/explore-data/SKILL.md)
and [validate-data](https://github.com/anthropics/knowledge-work-plugins/blob/main/data/skills/validate-data/SKILL.md).
Adopted grain, keys, coverage, join checks and conclusion validation. Omitted exhaustive default
profiles, fixed warning thresholds, connector assumptions and universal numeric range restrictions.
