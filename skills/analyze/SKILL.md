---
name: analyze
description: Answer questions from datasets, tables or query results by checking data quality, computing metrics and assessing trends, comparisons or statistical claims. Use for /analyze or analysis of supplied data. Code diagnosis, diff review and general web research use their own skills.
disable-model-invocation: true
---

# Analyze

Turn a bounded data question into a reproducible answer whose claims match the evidence. Use available
read-only sources and tools; analysis does not authorize changing source records or installing connectors.

## Frame and inspect

Identify the question, population, observation unit (one row per what), time window and requested
metric. Establish units, relevant time zone, denominator and comparison baseline before calculating.
Infer these from the source when possible; ask only when ambiguity materially changes the answer.
If the data is unavailable, explain what is missing and offer a method when useful; do not invent results.

Inspect schema and size before loading large inputs. Check the columns needed for the question for
missing values, types, key uniqueness and relevant time coverage. Scale checks to the task: a small
lookup needs enough verification to answer it, not an exhaustive profile of every column.
For unfamiliar/messy datasets, joins or time comparisons, read
[data quality and calculation checks](references/data-quality.md).

## Calculate and challenge

Use executable calculations with available Python, SQL or spreadsheet tools for nontrivial results.
Keep source files intact; use working copies or read-only queries. Record material filters, joins,
exclusions and transformations so the result can be reproduced. Do not silently drop duplicates or
outliers, replace missing values with zero, or impute data to obtain a plausible answer.

Check row counts and join cardinality, denominators, units and an independent aggregate or spot check
where appropriate. Reconcile totals only when the measure is additive. Distinguish unavailable data
from a measured zero. Preserve the difference between a population result and an estimate from a sample.

For inference, hypothesis testing, experiments, forecasts or outlier decisions, read
[statistical judgment](references/statistics.md). Separate observed differences from explanations;
a before/after comparison alone does not establish causation. Investigate anomalies before excluding them.

## Deliver

Answer in chat by default: the finding, verified figures, material limitations and enough method
(source, window, filters and denominator) to understand or reproduce it. State what the data cannot
answer. Do not force a report template or follow-up list onto a simple question.

Use the available native spreadsheet/artifact skill for requested files and its verification rules;
use appropriate visualization tools when a chart materially helps. Check chart scales, labels and
aggregation against the computed result. Dashboard implementation uses /ui. Extracting tables from
PDFs or other documents uses the available format skill before analysis; verify extraction fidelity.
Do not upload private data to a third-party service merely to calculate or visualize it.

Supporting references record method sources. For evaluation only, use
[scenarios](evals/evals.json); do not load them during ordinary analysis.
