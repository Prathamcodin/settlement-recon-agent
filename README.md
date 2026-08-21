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

- [x] Step 1: Synthetic data generator + ground truth (seed=42, 63 records)
- [x] Step 2: Deterministic matcher + tests (51/63 auto-matched, 81%)
- [x] Step 3: LLM classifier + live-recorded `evidence/before_guard.json`
      (Gemini 3.6 Flash, 11/12 correct, 0 false reconciliations)
- [ ] Step 4: Validation guard + tests + `evidence/after_guard.json`
- [ ] Step 5: Reporter + FastAPI + CLI
- [ ] Step 6: Dockerize + full README

## Safety thesis

The live batch on `gemini-3.6-flash` produced zero false reconciliations. That
is the honest result and we do not tune the prompt to force a failure. **The
guard exists as defense-in-depth**: 11/12 correct is not a scale-safe number,
and one wrong auto-reconcile moves real money. The guard re-checks every LLM
claim against hard arithmetic and ledger evidence before accepting it. Its
correctness is proven by [tests/test_guard.py](tests/test_guard.py) against
hand-crafted adversarial LLM-shaped fixtures in
[tests/fixtures/](tests/fixtures/) — the false reconciliations we did not
observe live but must reject if they ever appear.

## Run (through step 3)

```bash
python -m src.generator --seed 42 --out data/
python -m src.matcher --orders data/orders_ledger.csv --payouts data/settlement_file.csv
python scripts/capture_before_guard.py            # replay-mode; no API calls
```
