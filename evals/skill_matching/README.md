# Department classifier benchmark

Reference-based eval for `blueprint.matching.match_departments`: given the first 2–3
exchanges of a discovery conversation, which of the four department skills apply (1–3)?
The labels in `cases.yaml` are the judge; no LLM grades anything.

```bash
uv run python evals/skill_matching/run.py            # opus vs sonnet, 3 repeats (~$1)
uv run python evals/skill_matching/run.py --dry-run  # cost estimate only
```

## Cases

20 cases: three clean cases per department (12), six two-department overlaps, one
three-department case, and one deliberately ambiguous case (internal IT helpdesk, which is
not one of the four departments). Each case carries a `note` with the labeling rationale
so a disagreement can be resolved by reading, not guessing.

## Metrics

Multi-label, so plain accuracy is not enough:

- **exact match**: the predicted set equals the label.
- **mean Jaccard**: partial credit for overlapping sets.
- **over-selection rate**: predicted a department that is not in the label. The costlier
  error, because it injects irrelevant examples into the stakeholder's conversation.
- **under-selection rate**: missed a labeled department.
- **unstable cases**: the three repeats of a case did not all agree. A classifier that
  changes its answer on identical input changes which examples a stakeholder sees.
- per-department precision/recall, cost per call, p50/p95 latency.

## Result (2026-09-14, 20 cases × 3 repeats)

| | claude-opus-5 | claude-sonnet-5 |
|---|---|---|
| exact match | **95.0%** | 91.7% |
| mean Jaccard | 0.975 | 0.958 |
| over-selection | 5.0% | 8.3% |
| under-selection | 0% | 0% |
| unstable cases | **0** | 1 |
| mean cost / call | $0.0091 | $0.0054 |
| p50 / p95 latency | 2.7 s / 4.8 s | 2.2 s / 2.9 s |

Both models missed the same case identically on all six runs (`hr_onboarding_packets`,
returning `[hr, finance]` against a label of `[hr]`). Unanimous disagreement across two
models points at the label, not the models; on review the transcript names payroll's
process explicitly, so the case was relabeled `[hr, finance]`. With that correction Opus
is at 100% exact match on this set and Sonnet's only miss is the ambiguous IT-helpdesk
case, where it was also unstable (2 of 3 runs added `hr`).

**Decision: `claude-opus-5` stays the classifier model.** The accuracy gap is one case
(5% on a 20-case set, not statistically strong), but the stability gap is the signal that
matters for this job, and the cost difference is ~0.4¢ per conversation because the
classifier runs once. Sonnet would be the right choice if this ran per turn or at volume.

Raw per-call results, including each model's rationale, are written to `results/` (gitignored).
