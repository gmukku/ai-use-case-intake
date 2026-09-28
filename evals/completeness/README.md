# Completeness checker benchmark

Reference-based eval for `blueprint.completeness.assess_capture`: given one recorded canvas
summary, which rubric elements are genuinely still missing? The labels in `cases.yaml` are
the judge; no LLM grades anything.

```bash
uv run python evals/completeness/run.py             # 3 models x 2 repeats (~$1)
uv run python evals/completeness/run.py --dry-run   # cost estimate only
uv run python evals/completeness/run.py --models claude-sonnet-5 --repeats 1
```

## Why both error directions cost something

The checker runs *inside* the record tool, so its verdict becomes a follow-up question in the
same reply. That makes the two failures asymmetric but both real:

- **A missed gap** — the category is accepted while still thin, the spec ships incomplete, and
  a reviewer finds it later (or doesn't).
- **An invented gap** — the stakeholder is asked something they have already answered. Not
  dangerous, but it is the exact behaviour that makes a discovery conversation feel like a
  form, and the clarification budget only caps it at two rounds per category.

`missed_rate` is the headline; `invented_rate` is the tiebreak.

## Cases

21 labeled captures across all seven categories: at least one sufficient answer and one thin
answer per category, plus boundary cases that exist to catch over-strictness —

- `value_qualitative_but_specific`: a real measurable impact stated without a percentage. A
  checker that demands a number here nags a stakeholder who already answered.
- `systems_explicit_none`: "nothing else is involved" is a complete answer, not a gap. A
  checker that treats it as missing would loop asking for a system that does not exist.
- `activities_headcount_without_frequency`: two of three rubric keys present, deliberately
  near the boundary, to test that all three keys are read rather than general richness.

## Results (2026-09-28, 21 cases x 3 models x 2 repeats, $0.77)

| | opus | sonnet | haiku 4.5 |
|---|---|---|---|
| **missed gap rate** | **0.0%** | **0.0%** | **0.0%** |
| invented gap rate | 0.0% | 2.4% | 19.0% |
| exact match | 100.0% | 97.6% | 81.0% |
| unstable cases | 0 | 1 | 2 |
| mean cost/call | $0.0069 | $0.0042 | $0.0070 |
| p95 latency | 4.0s | 4.2s | 24.7s |

**All three miss nothing.** The safety-critical direction is tied, and the models separate
entirely on over-flagging.

**Haiku is ruled out.** 19% invented means roughly one capture in five produces a needless
follow-up, and it was *not cheaper* — a smaller model producing structured output cost the
same as opus and took 3.5x longer, with a p95 of 24.7 seconds inside a tool call. That would
make the conversation feel broken. Worth recording because "smaller model, lower cost" is the
assumption the benchmark existed to check, and it was wrong here.

**Opus stays the default.** Sonnet is genuinely viable — it holds the metric that matters at
40% less — but the gap is about 2¢ per conversation, opus is flawless and stable across
repeats, and an invented gap spends the one thing this project is trying to protect: the
stakeholder's patience. This is the same reasoning that kept opus as the classifier in step 2.
Set `BLUEPRINT_CHECKER_MODEL=claude-sonnet-5` to take the 40% if volume ever makes it matter.

Sonnet's single disagreement was `value_measure_without_problem`, where it also flagged
`measurable_impact` on a summary that states one ("the monthly error count in the audit
report"). Both of haiku's unstable cases were over-flagging too.
