# evidence/

Two artifacts live here, plus the raw recordings folder.

## `before_guard.json`

The LLM classifier's output on the seed=42 batch, scored against ground truth,
**without** the validation guard applied.

- Backend: `gemini-3.6-flash` (via the `LLMBackend` Protocol in
  [src/classifier/backend.py](../src/classifier/backend.py)).
- Prompt: a deliberately **fair** classification prompt — see
  [src/classifier/prompt.py](../src/classifier/prompt.py) — written to elicit
  correct answers, not to induce hallucination. `GENUINE_MISMATCH` is presented
  as a first-class outcome; FEE_VARIANCE carries an internal-consistency
  constraint; the model is told a downstream validator will re-check its claims.

**What the live run actually showed** on this batch (12 escalations):
- 11 / 12 correct classifications
- 1 wrong-category (a messy split labeled `GENUINE_MISMATCH` instead of
  `SPLIT_PAYOUT_WITH_VARIANCE` — conservative, not dangerous;
  `reconciles=false`)
- **0 false reconciliations** (both adversarial near-misses and the orphan
  payout were correctly identified as `GENUINE_MISMATCH`)

This is the honest result. We do not tune the prompt to force a failure — a
fair prompt on a capable model got this batch right. That is data.

## The safety thesis (why the guard still exists)

11/12 correct on 12 records is not a safety guarantee at production scale. One
wrong auto-reconcile moves real money. The validation guard is a **mandatory
invariant** — every LLM claim is re-checked against hard arithmetic and ledger
evidence before it is accepted — not a fallback for when the model misbehaves.

Concretely: this batch happened to produce zero false reconciliations. A
different batch, a different day, a different model version, or a different
prompt drift could produce a fabricated fee adjustment or an invented refund
tomorrow. The guard is what makes "trust the LLM" unnecessary. The evidence
that the guard *works* comes from the step-4 tests in
[tests/test_guard.py](../tests/test_guard.py), which feed hand-crafted
LLM-shaped adversarial responses (the kind we did not observe in the live
run) into the guard and prove each one is rejected against the arithmetic.

## `after_guard.json`

The guard's verdicts on the hand-crafted fixtures in
[tests/fixtures/](../tests/fixtures/) — adversarial LLM-shaped responses
that would be false reconciliations, plus positive controls that should
be accepted. Documents that the guard rejects the adversaries and
accepts the valid claims, with counts.

## `llm_raw/`

Verbatim recordings of the live Gemini responses that produced
`before_guard.json`. See [llm_raw/README.md](llm_raw/README.md) — the
recordings and the hand-crafted fixtures live in strictly separate
directories and never cross-populate.
