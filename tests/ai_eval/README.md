# AI SQL evaluation suite

Measures whether the NL→SQL agent (`py/ai/`) produces correct, safe SQL —
and whether a change to the prompt, the retriever, or the validator actually
improved things.

## Why coarse assertions

A question has many correct SQL answers, so comparing against one expected
string would fail on formatting alone. Instead each case asserts on the
properties that distinguish right from wrong: which tables were touched,
whether the query aggregates, whether a row cap survived, whether an id
appeared that the question never mentioned.

This catches every failure mode seen in production so far, and it runs today
without a database connection or a schema dump.

## Why not an existing eval framework

promptfoo, DeepEval, LangSmith and Braintrust all cover this ground, and for a
larger suite one of them would be the right call. This is hand-rolled for three
reasons.

**The assertions are domain-specific.** The failures this agent actually makes —
answering a sales question from the warehouse-quantity table, copying an id out
of the sample row in the prompt, losing the row cap when a subquery carries its
own `TOP` — are not expressible as built-in assertions anywhere. They would end
up as custom code inside whichever framework was adopted, which is most of the
work either way.

**`checks.py` has a second job.** It imports nothing from the codebase, so the
same assertions can replay historical runs out of `output/logs/ai_sql.log`
without touching the agent. A framework-bound assertion layer could not be
reused that way.

**Scale does not justify the dependency.** 26 cases and 55 tests run in half a
second with pytest and PyYAML, both already present. Adding a framework to a
production system means owning its version, its transitive dependencies and its
security surface.

Worth revisiting when any of these becomes true: the suite passes a few hundred
cases and needs dataset management; fuzzy cases need LLM-as-judge scoring;
several models have to be compared side by side; or results need a hosted
dashboard rather than JSON reports.

## Layout

```
tests/ai_eval/
  checks.py        assertion engine — pure logic, no project imports
  test_checks.py   tests for the engine itself
  cases.yaml       the cases
  run_eval.py      runner, writes JSON reports
  reports/         one JSON per run (gitignored)
```

`checks.py` deliberately imports nothing from the codebase. It can be
unit-tested standalone, and later reused to replay historical runs out of
`output/logs/ai_sql.log` without touching the agent.

## Running

```bash
# the engine's own tests — no DB, no model, no network
pytest tests/ai_eval/test_checks.py

# list cases without calling anything
python -m tests.ai_eval.run_eval --dry-run

# full suite (needs Ollama + a database connection)
python -m tests.ai_eval.run_eval

# one slice
python -m tests.ai_eval.run_eval --tags safety

# label a run so two reports can be compared
python -m tests.ai_eval.run_eval --label baseline
python -m tests.ai_eval.run_eval --label no-sample-rows
```

Exit code is 0 only when every case passes, so this can go into CI once the
pass rate is stable.

## Before the first real run

1. Replace the placeholder table names in `cases.yaml` (`ord_log`, `inv`,
   `inv_data`, `customer`, `plog`) with the real ones.
2. Confirm the dialect. `sql_validator.py` injects `TOP` and blocks `xp_`,
   `OPENQUERY`, `GO` — all SQL Server — while `setting.py` points at MySQL.
   The row-cap assertions accept either `TOP n` or `LIMIT n`; tighten them
   once the actual target is confirmed.
3. Run `--dry-run` first to check the cases load.

## Cases that are expected to fail at first

Several cases encode behavior the agent does not have yet. They are written
as failures on purpose — a failing case is the evidence for making the change.

| Case | What it exposes |
|---|---|
| `no_credential_exposure` | `validate_sql` has no table allowlist, so `users` (password hashes, reset tokens) is reachable |
| `ambiguous_what_happened` | the `_CORE` fallback hands over six tables when nothing scores above threshold, inviting confident nonsense instead of a clarifying question |
| `cap_survives_join` | `_inject_top` returns after capping the *first* `TOP` it finds, so a `TOP` inside a subquery satisfies the check while the outer query stays unbounded |
| `sales_top_sku_en`, `revenue_by_month` | `_RANK_HINT` covers Chinese ranking words well but English only via `top \d+`; "sold the most" and "total revenue" may skip the weak-SQL retry |
| `year_filter`, `quarter_filter` | `_tokens` drops bare digits of length ≤ 4, removing years and quarter numbers from retrieval scoring |

## Growing the suite

The fastest source of new cases is `output/logs/ai_sql.log` — every line is a
real question with its generated SQL and outcome. Failures there become cases
here, which keeps the suite anchored to problems that actually occurred rather
than ones imagined at the desk.

## Interpreting the report

`failure_histogram` buckets failures by kind, so a run-over-run diff shows
*which* kind of error moved. A prompt change that lifts the overall pass rate
while increasing `forbidden table present` has traded one problem for another
— the histogram makes that visible, the pass rate alone does not.
