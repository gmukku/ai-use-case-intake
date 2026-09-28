# Risk gate: false-negative check

Reference-based eval for `blueprint.review.assess_risk`. The gate decides whether a human
should look harder before the Builder touches anything, and CLAUDE.md asks specifically for
"a false-negative check on the risk gate: cases that should have been flagged for extra
review but weren't".

The rules are deterministic — no model is involved — so this suite costs **$0.00**, runs in
under a second, and is exact.

```bash
uv run python evals/risk_gate/run.py
uv run python evals/risk_gate/run.py --limit 5
```

It exits non-zero on any scrutiny false negative, so it can gate CI.

## Cases

18 labeled canvases. Each gives only the category summaries that matter; the rest are filled
with neutral text. Coverage is deliberately weighted toward the ways a rule quietly stops
firing:

- **Should be quiet** (4): a plain summarizer, email-only chasing, a Q&A chatbot over the
  team's own documents, and a stakeholder saying "it doesn't touch any other systems" in
  their own words rather than the regex's.
- **Named systems** (6): a product from a skill's list, generic types (`HRIS`, `CRM`, `ERP`),
  and SharePoint as a read-only source.
- **Writes** (4): into a named product, via "push"/"create" and "update", and one negated
  ("never writes back") that must *not* fire.
- **Sensitive, external, automation, gaps** (4): each flag on its own, so a rule that breaks
  is attributable to itself rather than hidden behind another flag on the same case.

Every case carries a `note` with the labeling rationale, so a disagreement is settled by
reading rather than guessing.

## Metrics

The headline is the **scrutiny false negative**: a case a human says deserves extra scrutiny
that the rules waved through. The asymmetry is the whole point — a false positive costs a
reviewer thirty seconds, a false negative is an unreviewed build. Also reported:

- `scrutiny_recall`, `scrutiny_false_positives`
- `exact_match` on the flag set, `flags_missed`, `flags_spurious`
- per-flag precision / recall / missed

## What the first run found

Zero false negatives, and three flags with **recall 0.00** — `writes_to_system`,
`workflow_automation` and `external_sources` were not firing at all:

- `writes_to_system` matched against a **hand-written list of system nouns** that had drifted
  from reality. 74 of the systems the department skills name (BambooHR, Greenhouse,
  QuickBooks, ServiceNow…) and 8 of the generic types in `_GENERIC_SYSTEMS` were invisible to
  it, while `named_integration` — which derives its names from the skills — matched them all.
  A spec that wrote into BambooHR was being shown as `elevated` rather than `high`.
  The rule now derives its targets from the same two sources, so the lists cannot diverge.
- It also required a preposition: "update the ERP" did not match because there was no
  into/to/in. That is how people actually phrase it.
- `workflow_automation` was word-order dependent: "automatically route" matched,
  "route it automatically" did not.
- `external_sources` matched "external website" but not "the government website".

None of these produced a *scrutiny* false negative, because `named_integration` had already
elevated those cases — which is exactly why they had survived: a second flag was covering for
them. Each fix is now also a unit test in `tests/test_review.py`.

Three labels were corrected by the evidence rather than the code: `needs_extra_scrutiny` is
`level >= elevated`, and I had labeled sensitive-data, external-source and automation cases as
not needing scrutiny on the assumption it was reserved for named integrations. CLAUDE.md names
integrations as a case that must reach it, not the only one.

Current: 18/18 exact, 0 false negatives, 0 false positives.
