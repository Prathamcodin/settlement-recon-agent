# Settlement Reconciliation Agent

**Razorpay Buildathon — Track 04 (AI Finance Controller).**

A settlement reconciliation agent that matches a payment aggregator's payout file
against a merchant's internal orders ledger, auto-reconciles what it can with
deterministic logic, and produces an honest exception list for what it can't.

## Layered architecture (this is the whole idea)

```
1. Deterministic matcher (trusted core, no LLM)
      └── clears the clean majority
2. LLM exception classifier (bounded, untrusted)   [Gemini 2.5 Flash]
      └── explains WHY the residue doesn't reconcile
3. Validation guard (safety layer)
      └── re-checks every LLM claim against hard arithmetic
4. Reporter + audit trail
      └── match rate, reason codes, per-record log
```

The LLM sits behind a small `LLMBackend` Protocol
([src/classifier/backend.py](src/classifier/backend.py)) so the provider is
pluggable — the default is Gemini 2.5 Flash (Google's free tier, `GEMINI_API_KEY`),
but any implementation of the Protocol drops in without touching the prompt,
the guard, or the reporter.

The LLM never invents a match. Its output is treated as untrusted and gated by
the validation guard — if the numbers don't support its explanation, the
classification is rejected.

## Status

- [x] Step 1: Synthetic data generator + ground truth
- [ ] Step 2: Deterministic matcher + tests
- [ ] Step 3: LLM classifier (pre-guard) + `evidence/before_guard.json`
- [ ] Step 4: Validation guard + tests + `evidence/after_guard.json`
- [ ] Step 5: Reporter + FastAPI + CLI
- [ ] Step 6: Dockerize + full README

## Run (step 1)

```bash
python -m src.generator --seed 42 --out data/
```

Produces `data/orders_ledger.csv`, `data/settlement_file.csv`, and
`data/ground_truth.json` (the last is read only by tests / final accuracy
report, never by the reconciler itself).
