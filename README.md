# Settlement Reconciliation Agent

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests 81 passing](https://img.shields.io/badge/tests-81_passing-brightgreen.svg)](tests/)
[![Razorpay Buildathon — Track 04](https://img.shields.io/badge/Razorpay_Buildathon-Track_04-orange.svg)](#)

Matches merchant orders against aggregator settlements, auto-reconciles the clean
majority with deterministic logic, and boxes the LLM in with a validation guard
so it cannot hallucinate money into balancing. Every run can be saved to a
MongoDB audit store and queried afterwards through the API.

## Results on the reference batch (63 records)

| Layer | Result | Framing |
|---|---|---|
| Deterministic matcher | **51 / 63** auto-matched (81%) | Pure logic ceiling — no LLM involved |
| LLM classifier (Gemini 3.6 Flash, live) | **0** false reconciliations across 12 escalations | *This batch went clean — but that's not a safety guarantee at scale.* One wrong auto-reconcile moves real money. |
| Validation guard | **6 / 6** adversarial fabrications rejected — including one arithmetically-perfect **"smart lie"** | The mandatory invariant that makes the system trustworthy regardless of what the LLM does on the next batch. |

At scale (no LLM calls involved):
- **Matcher:** 100,800 synthetic records matched in **1.9 s** with **0 false
  auto-matches**, and the whole run saved to MongoDB in **3.9 s**
  ([Scale benchmark](#scale-benchmark)).
- **Guard:** **34,000 / 34,000** mechanically generated lies rejected and
  **16,000 / 16,000** legitimate claims accepted, 50,000 cases across 5 seeds
  ([Guard at 50,000 cases](#guard-at-50000-cases)).

## Run it — no API key, no network

```bash
python run.py
```

Regenerates data from the seeded generator, runs the deterministic matcher,
replays the 12 committed LLM recordings, runs the guard, prints the summary.
Requires `pip install -r requirements.txt` (Python 3.11+), but **no API key
and no network calls are needed at runtime** — the classifier replays from
the committed recordings in `evidence/llm_raw/`.

Docker:

```bash
docker compose up
```

Same result. Same reproducibility guarantee.

## What the summary prints

```
==================================================================
 SETTLEMENT RECONCILIATION AGENT  --  BATCH RUN
==================================================================

DETERMINISTIC MATCHER (src/matcher)
  total decisions       : 63
  MATCHED               :  51  (81.0%)
  ESCALATED             :  12  (19.0%)

LLM CLASSIFIER (Gemini 3.6 Flash; replay from evidence/llm_raw/)
  escalations classified: 12
  LLM says reconciles   : 8
  LLM says exception    : 4

VALIDATION GUARD (src/guard)
  accepted              : 12
  rejected              : 0

Final category breakdown:
  MATCHED                          51
  FEE_VARIANCE                     5
  GENUINE_MISMATCH                 4
  PARTIAL_REFUND                   2
  TIMING_WINDOW                    1

==================================================================
 TWO-LABEL SAFETY FINDING
==================================================================
Live batch:  LLM produced 0 false reconciliations across 12 escalations
             (accuracy 11/12 vs ground truth; source: evidence/before_guard.json)
Guard test:  6/6 adversarial fabrications rejected on hand-crafted fixtures,
             including one arithmetically-perfect 'smart lie' (source: evidence/after_guard.json)
==================================================================
```

## The safety thesis — why the guard exists

**A capable model got 11 / 12 on this batch. That is exactly why the guard must
exist** — because one wrong auto-reconcile moves real money. The guard is not a
fallback for when the LLM misbehaves; it is a mandatory invariant that runs on
every classification, verified against ten hand-crafted fixtures (six
adversarial lies and four legitimate cases), including the hardest one below.

### The "smart lie"

On the `ORD-0006` near-miss (`gross_amount=Rs.4823.00`, `fee_charged=Rs.96.46`,
`settled_amount=Rs.4703.20` — a Rs.23.34 gap), the dangerous LLM output is the
one whose arithmetic looks perfect:

| Field | Value |
|---|---|
| What the LLM proposed | `FEE_VARIANCE` with `actual_fee = Rs.119.80` — perfect math: `4823.00 − 119.80 = 4703.20 = settled_amount` |
| What the settlement recorded | `fee_charged = Rs.96.46` (not Rs.119.80) |
| **Guard verdict** | **REJECTED** → forced to `GENUINE_MISMATCH`. The LLM's proposed `actual_fee` disagrees with the settlement row's actual `fee_charged` by Rs.23.34, which exceeds the tolerance of Rs.4.82. |

The guard's audit trail records the exact reason:

> *"The LLM's proposed actual_fee Rs.119.80 would arithmetically close the gap
> if used in place of fee_charged, but it disagrees with the payout row's actual
> fee_charged Rs.96.46 by Rs.23.34 (> tolerance Rs.4.82). The guard validates
> against the settlement's recorded fee, not the model's proposal."*

**The pitch point.** A naive validator would accept the smart lie because the
LLM's arithmetic balances. The guard validates against the settlement row's own
`fee_charged`, not against numbers the model produced. That is what makes the
LLM safely bounded — not the prompt, not the model choice, not any prayer that
the model gets it right; the fact that its numerical claims are re-checked
against ground-truth data every time.

Fixture: [tests/fixtures/adversarial_selfconsistent_fee_lie.json](tests/fixtures/adversarial_selfconsistent_fee_lie.json)
· Full audit trail: [evidence/after_guard.json](evidence/after_guard.json)

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  1. DETERMINISTIC MATCHER  (trusted core, no LLM)               │
│     Groups payouts by order_ref; closes exact / within-tolerance│
│     / clean-split cases. 51/63 on the reference batch.          │
└──────────────────────┬──────────────────────────────────────────┘
                       │  the 12 that don't close deterministically
                       v
┌─────────────────────────────────────────────────────────────────┐
│  2. LLM EXCEPTION CLASSIFIER  (bounded, untrusted)              │
│     Behind LLMBackend Protocol → provider-pluggable.            │
│     Default: Gemini 3.6 Flash. Replay-by-default from           │
│     evidence/llm_raw/ for reproducibility.                      │
└──────────────────────┬──────────────────────────────────────────┘
                       │  every classification, with its claim
                       v
┌─────────────────────────────────────────────────────────────────┐
│  3. VALIDATION GUARD  (safety layer, invariant not fallback)    │
│     Re-checks arithmetic + ledger status against raw settlement │
│     data. Rejects any claim that doesn't close, including the   │
│     "smart lie" where the LLM's math is self-consistent but     │
│     contradicts fee_charged.                                    │
└──────────────────────┬──────────────────────────────────────────┘
                       v
┌─────────────────────────────────────────────────────────────────┐
│  4. REPORTER + AUDIT TRAIL                                      │
│     Merges the three layers by key. Final categories + per-     │
│     record log. CLI summary + JSON report + API response.       │
└─────────────────────────────────────────────────────────────────┘
```

**Only layer 2 is probabilistic.** Layers 1, 3, and 4 are pure Python. The LLM
sits behind a tiny `LLMBackend` Protocol
([src/classifier/backend.py](src/classifier/backend.py)) so swapping providers —
Gemini, Claude, self-hosted, whatever — is one class, three attributes, no
changes anywhere else.

## Repo tour

| Path | Layer / role |
|---|---|
| [src/generator/](src/generator/) | Seeded synthetic-data generator (63 records, controlled scenario mix; `--scale N` multiplies it) |
| [src/matcher/](src/matcher/) | Layer 1 — deterministic matcher + precedence rules |
| [src/classifier/](src/classifier/) | Layer 2 — LLM classifier, `LLMBackend` Protocol, `GeminiBackend`, record/replay mechanics |
| [src/classifier/prompt.py](src/classifier/prompt.py) | The fair strict-JSON classification prompt |
| [src/guard/](src/guard/) | Layer 3 — validation guard with per-category invariant checks |
| [src/reporter/](src/reporter/) | Layer 4 — report builder + text summary formatter |
| [src/api/](src/api/) | FastAPI `/reconcile` surface (replay-only) |
| [src/tolerance.py](src/tolerance.py) | `max(Rs.0.50, 0.1% × gross)` — shared by matcher, guard, generator |
| [src/models.py](src/models.py) | Shared dataclasses: `Order`, `Payout`, `MatchDecision` |
| [src/store/](src/store/) | MongoDB audit store: every run and its per-record audit trail, queryable |
| [tests/](tests/) | 81 tests (+5 that run against a real MongoDB): matcher, classifier, guard, reporter, API, store, generator |
| [tests/fixtures/](tests/fixtures/) | Hand-crafted LLM-shaped adversarial + positive fixtures |
| [evidence/](evidence/) | Live-recorded LLM output + before/after-guard artifacts |
| [scripts/](scripts/) | Evaluation scripts (`capture_before_guard.py`, `capture_after_guard.py`, `scale_benchmark.py`, `stress_guard.py`) |
| [run.py](run.py) | Top-level pipeline entrypoint |

## Evidence artifacts

- **[evidence/before_guard.json](evidence/before_guard.json)** — scoring of the
  live Gemini 3.6 Flash run against ground truth: 11/12 correct classifications,
  0 false reconciliations across 12 escalations. The single miss (ORD-0043, the
  messy split) is a **conservative** error: the model classified it as
  `GENUINE_MISMATCH` instead of `SPLIT_PAYOUT_WITH_VARIANCE` — it declined to
  auto-reconcile rather than falsely reconciling, so its one error errs toward
  caution and lands the record in the human-review queue, which is the safe side.
- **[evidence/after_guard.json](evidence/after_guard.json)** — the guard run
  against the 10 hand-crafted fixtures: 6/6 adversarial rejected, 4/4 positive
  accepted, per-check audit trail for every fixture.
- **[evidence/llm_raw/](evidence/llm_raw/)** — 12 verbatim recordings from the
  live capture, one JSON per escalation. Never hand-edited. Strictly separate
  from `tests/fixtures/` (see [evidence/llm_raw/README.md](evidence/llm_raw/README.md)).
- **[evidence/heldout_guard.json](evidence/heldout_guard.json)** — held-out
  adversarial stress test (see next section): 170/170 mechanically-generated
  smart lies rejected and 80/80 legitimate positive controls accepted across
  140 randomized records at seed=1337.
- **[evidence/guard_stress.json](evidence/guard_stress.json)** — the same suite
  at 50,000 cases across 5 seeds: 34,000/34,000 lies rejected, 16,000/16,000
  legitimate accepted (see [Guard at 50,000 cases](#guard-at-50000-cases)).

## Held-out adversarial stress test

Complementary evidence to `after_guard.json` (which uses the ten hand-authored
fixtures). This suite proves the guard on data **the author did not choose**:

- **Randomized records** — a distinct seed (`1337`, vs the main batch's `42`),
  randomized gross amounts, randomized fee rates drawn from a realistic table,
  randomized which-scenario. 140 records generated.
- **Mechanically synthesized lies** — for each unresolvable record, the
  generator produces the arithmetically-self-consistent smart lie
  parametrically: `actual_fee = gross − settled` (so `gross − actual_fee ==
  settled` balances perfectly) but contradicts the settlement row's real
  `fee_charged`. Also: wrong-status refund lies, ghost-split lies, orphan
  claims. 170 adversarial cases.
- **Positive controls** — 80 legitimate cases (30 fee-variance, 30 partial-
  refund, 20 splits) prove the guard is not simply rejecting everything.

The suite pipes every case through the actual guard
([src/guard/verify.py](src/guard/verify.py), unchanged) and asserts the
verdict.

**Result on the current guard**: 170/170 adversarial rejected, 80/80 legit
accepted. See [evidence/heldout_guard.json](evidence/heldout_guard.json) for
the per-case audit trail.

**Scope statement (honest).** This stress-tests the *modeled dangerous-lie
class* — arithmetically-self-consistent fabrications that contradict the
settlement row, ledger-status refutations, ghost-leg splits, and orphan
claims — across randomized inputs. It does **not** claim to cover every
possible novel lie type. Categories the guard cannot verify at all fail
closed: an unknown category is always rejected, on principle (see the
`known_category` check in `src/guard/verify.py`).

Regenerate any time with `python scripts/capture_heldout_guard.py` (no key,
no network, ~0.05s).

### Guard at 50,000 cases

`scripts/stress_guard.py` runs the same held-out suite 40x larger on five
different seeds, through the unchanged guard, and records any case it gets
wrong ([evidence/guard_stress.json](evidence/guard_stress.json)):

| Case type | Cases | Guard result |
|---|---|---|
| Smart fee lie (arithmetic closes, contradicts `fee_charged`) | 10,000 | all rejected |
| Refund lie on an order the ledger says is `captured` | 10,000 | all rejected |
| Ghost split (invented payout legs) | 10,000 | all rejected |
| Fee / refund claims on an orphan payout | 4,000 | all rejected |
| Legitimate fee variance, partial refund, split | 16,000 | all accepted |
| **Total** | **50,000** | **0 mistakes** (1.9 s) |

Same scope as above: more randomized amounts, fee rates and seeds for the
modeled lie types, not new kinds of lies. What the larger run adds is
evidence that the checks don't depend on the particular 140 records at
seed 1337; the original 250-case suite is unchanged.

```bash
python scripts/stress_guard.py                       # 50,000 cases
python scripts/stress_guard.py --scale 4 --seeds 7   # quick run
```

## Recording new evidence (advanced)

The default `python run.py` replays committed recordings. To recapture — after
changing the batch, the prompt, or the model — set a Gemini API key and pass
`--record-live`:

```bash
# One-time: copy .env.example to .env and paste your Gemini API key
# Free tier: https://aistudio.google.com/apikey
python run.py --record-live
```

This makes ~12 API calls to `gemini-3.6-flash` (free tier), writes verbatim
responses to `evidence/llm_raw/`, and prints the summary. Total cost is
negligible; total wall-clock is under a minute.

The recordings are the pitch evidence and belong in the commit alongside the
prompt that produced them.

## FastAPI `/reconcile`

```bash
docker compose --profile api up      # starts MongoDB + the API
# then:
curl http://localhost:8000/reconcile/sample | jq
```

Endpoints:

- `GET /` — health + capability description
- `GET /reconcile/sample` — runs the pipeline on the committed batch (no upload)
- `POST /reconcile` — accepts multipart uploads of `orders` + `settlements`
  CSVs; runs the full pipeline; returns the JSON report
- `GET /runs`, `GET /runs/{run_id}`, `GET /runs/{run_id}/decisions`,
  `GET /runs/{run_id}/decisions/{key}` — query saved runs
  (see [Audit store](#audit-store-mongodb))

**Design note.** The matcher and the guard execute **live** on any uploaded
CSVs — pure logic, no cache. The LLM classifier is **replay-only** — reads from
`evidence/llm_raw/`. If an uploaded batch escalates a record whose key has no
cached response, the endpoint returns HTTP 422 with the missing key. This is a
deliberate reproducibility boundary, not a bug: the service cannot go rogue on
demo day, cannot generate new bills, and cannot silently accept whatever the
model says next. Recording new evidence remains a CLI operation
(`run.py --record-live`) where a human is present to approve API calls.

## Audit store (MongoDB)

A report printed once can't answer the questions a reconciliation team asks
afterwards: *"which records in Tuesday's batch were forced to
GENUINE_MISMATCH?"*, *"show me the full audit trail for ORD-0006"*. When
`MONGO_URL` is set, every run (API or `python run.py --save`) is saved to
MongoDB and the `/runs` endpoints query it. Without `MONGO_URL` everything
works as before and `/runs` answers 503.

```bash
docker compose --profile api up -d mongo
MONGO_URL=mongodb://localhost:27017 python run.py --save
MONGO_URL=mongodb://localhost:27017 uvicorn src.api.app:app
```

```
POST /reconcile                                      -> {..., "run_id": "633be0..."}
GET  /runs                                           -> past runs, newest first
GET  /runs/{run_id}                                  -> counts + category breakdown
GET  /runs/{run_id}/decisions?category=FEE_VARIANCE  -> the 5 fee-variance records
GET  /runs/{run_id}/decisions?exceptions_only=true   -> everything not MATCHED
GET  /runs/{run_id}/decisions/ORD-0006               -> full audit trail for one record
```

**Why a document store.** One decision's audit trail is a nested document
whose shape depends on its category: a clean match has no LLM fields at all;
an escalation carries the LLM's claim (`actual_fee` for a fee variance,
`refund_amount` for a refund, leg ids for a split) and a variable-length list
of guard checks. In MongoDB it is stored and read back exactly as the
reporter builds it. In SQL it would need separate tables for decisions,
claims and guard checks, joined on every read, plus a JSON column for the
free-form claim anyway.

**Two collections** ([src/store/mongo_store.py](src/store/mongo_store.py)):

| Collection | One document per | Indexes |
|---|---|---|
| `runs` | batch run: source, time, counts, input fingerprint | `created_at` (newest-first listing), `input_fingerprint` |
| `decisions` | record in a run, with its full audit trail | `(run_id, key)` **unique**, `(run_id, final_category)` |

Every query is scoped to one run, so `run_id` leads both indexes. The unique
index makes "one decision per record per run" a database rule, not just a
convention. On the 100,800-record run, the exception query is served by the
`run_category` index in **6.7 ms** (checked with `explain()`).

**Money is never a float, here either.** Enforced at both ends:

- **At the source.** The classifier parses the LLM's JSON with
  `parse_float=str`. The prompt asks for money as decimal strings
  (`"actual_fee": "63.42"`); a model that returns a JSON number
  (`63.42`) still yields the exact text `"63.42"`, never a float.
- **At the store.** `Decimal` is written as BSON `Decimal128` (exact) and
  read back as a decimal string; decimal strings are stored verbatim. A
  **float is rejected, not converted**: by the time a float exists its
  precision may already be gone (`0.1 + 0.2` is `0.30000000000000004`), and
  converting it would only record that error exactly. `save_run` raises
  `TypeError` naming where the float was (e.g. `ORD-B.llm_claim.actual_fee`),
  and it checks every document **before the first write**, so a rejected
  report leaves nothing behind, not even a `"writing"` run.

Tested on mongomock and on a real MongoDB.

**Partial writes.** A run is inserted with `status: "writing"`, its decisions
go in with one bulk insert, then the run flips to `"complete"`. Readers only
see complete runs, so a crash halfway through a save never shows up as a run
with missing decisions. A multi-document transaction would also work, but
needs MongoDB running as a replica set.

**Abandoned runs.** A run still `"writing"` an hour after it started belongs
to a process that died (saving 100,800 decisions takes about 4 seconds).
`purge_abandoned_runs()` deletes those runs and their partial decisions, and
runs every time the store connects. It deletes the **decisions first and the
run last**: if it dies in between, what's left is a stale `"writing"` run with
no decisions, which the next purge removes. The other order could leave
decisions whose run is gone, which nothing would ever find again (a test
interrupts the purge halfway to check this). A TTL index wouldn't do: TTL
deletes documents one collection at a time, so it would remove the run and
leave its decisions orphaned.

**Re-running the same input.** Reconciling the same files twice creates two
runs, on purpose: an audit log records every attempt, and a re-run after a
guard or prompt change can legitimately reach different verdicts. Each run
stores `input_fingerprint`, a SHA-256 of the batch content that ignores row
order, so repeats are visible: the response says
`"previous_runs_with_same_input": 1`, and
`GET /runs?input_fingerprint=...` lists every run of that input.

**Tests.** The store and endpoints are tested against `mongomock` (in-memory,
no server) in the normal suite, and against a real MongoDB in
[tests/test_store_integration.py](tests/test_store_integration.py), which runs
when `MONGO_URL` is set.

## Scale benchmark

The reference batch is 63 records. `scripts/scale_benchmark.py` generates the
same scenario mix multiplied (`--scale 1600` gives 100,800 reconciliation
units), runs the deterministic matcher, and scores every decision against
the generator's ground truth.

```bash
python scripts/scale_benchmark.py                                      # matcher only
MONGO_URL=mongodb://localhost:27017 python scripts/scale_benchmark.py  # + MongoDB timings
```

Result on one laptop (MongoDB 7 in Docker on the same machine),
[evidence/scale_benchmark.json](evidence/scale_benchmark.json):

| Measure | 100,800 records |
|---|---|
| Matcher | **1.9 s** (~53,000 records/s) |
| Auto-matched | 81,600 (81.0%), the same rate as the 63-record batch |
| False auto-matches (closed a record that needed review) | **0** |
| Missed matches (escalated a record it could have closed) | 0 |
| MongoDB: save the run (100,800 documents) | **3.9 s** (~25,700 docs/s) |
| MongoDB: first 100 GENUINE_MISMATCH records | 6.7 ms, via the `run_category` index |
| MongoDB: category breakdown (aggregation) | 121 ms |

**Scope.** This covers the deterministic layers. The LLM classifier is not
called at this scale: escalations have no recorded responses, and thousands
of live calls would cost money and could not be replayed. So escalations take
the pipeline's fail-closed path (no classification means GENUINE_MISMATCH, a
human reviews it). The guard is stress-tested separately on 170 generated
lies (see [Held-out adversarial stress test](#held-out-adversarial-stress-test)).

### A matcher bug the scale run found

The first 100,800-record run showed **72 false auto-matches**, none of which
appear in the 63-record batch. All 72 were fee variances on small orders
(around ₹100–140): the merchant expected a flat 2% fee, the aggregator
charged 2% + GST, and on a ₹138 order that difference is ₹0.50, inside the
₹0.50 rounding floor. The matcher absorbed it as rounding.

But rounding never changes the fee. A single payout now auto-matches only if
`fee_charged` equals the order's `expected_fee`; otherwise it escalates as
`fee_mismatch`, however small the gap, and the classifier and guard handle
it as a fee variance. After the fix: **0 false auto-matches** at 10,080 and
100,800 records, and the 63-record batch still matches exactly 51
(regression tests in [tests/test_matcher.py](tests/test_matcher.py)).

The scaled generator also needed one fix: orphan payouts used random
`ORD-9xxx` refs, which collide with real order ids past ~9,100 orders and
with each other. Scaled batches give orphans their own sequence; scale 1 is
byte-identical to before, so the committed LLM recordings still line up.

## Tests

```bash
python -m pytest tests/
```

**81 tests, all green**, plus 5 that run against a real MongoDB when
`MONGO_URL` is set. On a fresh clone the reference batch is generated
automatically ([tests/conftest.py](tests/conftest.py)). Coverage by layer:

| Suite | Tests | What they prove |
|---|---|---|
| `test_matcher.py` | 15 | Exact/tolerance/split-sum match paths, late-leg precedence, orphan payouts, out-of-window escalation, ground-truth-isolation invariant, fee change inside tolerance escalates as `fee_mismatch` |
| `test_classifier.py` | 13 | Record/replay mechanics, backend Protocol satisfaction via fake, safe-filename handling, missing-response error |
| `test_guard.py` | 15 | Parametrized over every fixture (adversarial rejected, positive accepted), smart-lie reason string, reconciles=false counterfactual logging, unknown-category rejection |
| `test_reporter.py` | 4 | Report shape, guard-reject rendering, **two-labeled-facts invariant** (Live batch and Guard test as separate lines), graceful missing-evidence handling |
| `test_api.py` | 9 | Root advertises replay-only, `GET /reconcile/sample` returns stable numbers, `POST /reconcile` round-trips uploads, replay-miss returns 422, runs saved and queried through `/runs`, re-runs of one input linked by fingerprint, 404s, 503 without a store |
| `test_store.py` | 17 | Save and query runs, aggregation agrees with the report, category and exception filters, full audit trail, run isolation, half-written runs hidden, abandoned runs purged (in-progress ones kept, no orphans if the purge dies halfway), Decimal and decimal strings stored exactly, **saving a float raises and writes nothing**, LLM JSON numbers parsed as exact text, input fingerprint, indexes |
| `test_generator_scale.py` | 3 | `--scale` multiplies every scenario, scaled keys are unique, scale 1 is byte-identical to the reference batch |
| `test_store_integration.py` | 5 | Real MongoDB: round trip and aggregation, exception query uses the `run_category` index, unique key per run enforced, exact Decimal128 money and float rejection, abandoned-run purge (skipped without `MONGO_URL`) |
| `test_heldout_guard.py` | 5 | Held-out suite generates >=100 adversarial + >=30 legit cases, guard rejects all 170 mechanically-generated smart lies at seed=1337, guard accepts all 80 legitimate positive controls, seed reproducibility, 3,000 more cases across 3 other seeds |

## What we deliberately did NOT do

- **The reconciler never reads `data/ground_truth.json`.** That file exists
  only for tests and evaluation scripts. The invariant is enforced by
  [tests/test_matcher.py::test_matcher_module_does_not_import_generator](tests/test_matcher.py),
  which purges cached imports and verifies the matcher does not pull in the
  generator (or, transitively, ground truth) as a side effect. The matcher
  measures the world; the ground truth is only for scoring what it measured.
- **We do not tune the prompt.** The classification prompt (see
  [src/classifier/prompt.py](src/classifier/prompt.py)) is written the way an
  engineer would write it to elicit correct answers — category definitions
  carry internal-consistency constraints, `GENUINE_MISMATCH` is a first-class
  outcome, and the model is told a downstream validator will re-check its
  claims. A prompt that led the model toward or away from an answer would
  invalidate the finding either way. This is a principle, not a defense.
- **We did not build a system that trusts the model.** The design assumes the
  LLM will eventually be wrong. The validation guard runs on every
  classification, verified against adversarial fixtures. The LLM's
  `reconciles=false` flag is never treated as sufficient evidence — the guard
  still runs the arithmetic and logs the counterfactual. Provider swaps are one
  Protocol implementation; safety does not depend on the specific model.
- **We did not stage a fake "before" failure.** The live batch really did
  produce 0 false reconciliations, and we report that honestly. The guard's
  correctness lives in the adversarial fixtures ([tests/fixtures/](tests/fixtures/)),
  not in a manufactured live failure. If the live run had produced a
  hallucination, we would have kept it too — the point is that we do not
  weaken the prompt or swap models to force one outcome.
- **No ML training, no fine-tuning.** Deterministic layers are pure Python.
  The LLM is used off-the-shelf; only the prompt is engineering.

## Build story

Eight git commits tell the build history:

```
89f048e  Step 1: synthetic data generator + ground truth
9dd910e  Step 2: deterministic matcher + unit tests
fbec32b  Step 3a: LLM classifier scaffolding (no live output yet)
8c455a6  Step 3a bis: swap LLM backend to Gemini 2.5 Flash behind a Protocol
ded70d2  Step 3b: live LLM capture (Gemini 3.6 Flash) + honest framing
         Note: the model string moved from gemini-2.5-flash (in the 3a-bis
         commit) to gemini-3.6-flash during 3b because Google retired 2.5
         Flash for new API keys mid-build; the code, prompt, and Protocol
         are unchanged, only DEFAULT_MODEL_ID and the SDK migrated
         (google-generativeai -> google-genai).
324f31a  Step 4: validation guard + adversarial fixtures + after_guard.json
8d695a8  Step 5: reporter + run.py + FastAPI /reconcile
db1f261  Step 6: Dockerize + full README
```

Each step passes its own tests and is checkoutable independently. Later
commits add the held-out adversarial suite, rich terminal output, and the
MongoDB audit store with the scale benchmark.
