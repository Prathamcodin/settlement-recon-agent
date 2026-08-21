# tests/fixtures/

**Hand-crafted LLM-shaped responses used to exercise the validation guard.**

These files are NOT real model output. They were written by the author of the
system to stress-test the guard's invariants — six adversarial cases the guard
must reject, and four positive controls it must accept. If you find yourself
wanting to hand-edit a file in `evidence/llm_raw/`, write a fixture here
instead: that directory holds only verbatim recordings and must never mix
with hand-authored data.

## Format

Each fixture is a JSON envelope:

```json
{
  "name": "<slug>",
  "kind": "adversarial" | "positive",
  "expected_guard": "REJECT" | "ACCEPT",
  "description": "<one-paragraph rationale>",
  "context": {
    "key": "ORD-…",
    "matcher_strategy": "amount_mismatch" | "split_incomplete" | …,
    "order": { … } | null,          // null for orphan payouts
    "in_window_payouts":  [ { … } ],
    "out_of_window_payouts": [ { … } ]
  },
  "llm_response": {
    "category": "FEE_VARIANCE" | …,
    "reconciles": true | false,
    "reasoning": "…",
    "claim": { … }
  }
}
```

The test in `tests/test_guard.py` parametrizes over every fixture and asserts
the guard's `accepted` flag matches `expected_guard`. The script
`scripts/capture_after_guard.py` runs the guard over every fixture and writes
the outcomes to `evidence/after_guard.json`.

## Adversarial fixtures (guard MUST REJECT each)

| File | Attack pattern | Why the guard rejects |
|---|---|---|
| `adversarial_fabricated_fee.json` | LLM invents an actual_fee that doesn't even balance | settlement internally inconsistent AND LLM's math also fails |
| `adversarial_selfconsistent_fee_lie.json` | **The "smart lie."** LLM proposes `actual_fee=119.80` so `gross − actual_fee == settled` PERFECTLY. Anchored on ORD-0006 (from the live batch). | Guard validates against the settlement row's `fee_charged`, not the model's proposed number. `actual_fee (119.80) ≠ fee_charged (96.46)` beyond tolerance. |
| `adversarial_invented_refund_wrong_status.json` | LLM proposes a refund whose arithmetic works, but ledger status is `captured`. Anchored on ORD-0011. | Refund requires `status ∈ {partially_refunded, refunded}`; ledger contradicts. |
| `adversarial_invented_refund_wrong_arithmetic.json` | Ledger status is `partially_refunded` (status check passes), but LLM's proposed `refund_amount` doesn't close the gap. | Refund arithmetic fails on settlement's actual fee_charged. |
| `adversarial_ghost_split_legs.json` | LLM cites `leg_payout_ids` that don't exist in this order's in-window payouts. | Legs must reference real in-window payouts for this order. |
| `adversarial_fake_timing.json` | LLM claims TIMING_WINDOW while an in-window payout exists. | TIMING requires zero in-window payouts by definition. |

## Positive controls (guard MUST ACCEPT each)

| File | Why it should pass |
|---|---|
| `positive_valid_fee_variance.json` | Internally consistent settlement, `actual_fee == fee_charged`. |
| `positive_valid_partial_refund.json` | `status=partially_refunded`, refund closes arithmetic. |
| `positive_valid_split.json` | Two legs summing to `net_expected` within tolerance. |
| `positive_llm_says_genuine_mismatch.json` | LLM said `GENUINE_MISMATCH`; guard accepts trivially. |

## Strict separation from `evidence/llm_raw/`

`evidence/llm_raw/` = verbatim recordings of live Gemini output, captured once
by `python -m src.classifier --mode record`. These files here in
`tests/fixtures/` = hand-authored envelopes. The two directories do not share
files, and the fixture loader/tests never load from `evidence/llm_raw/`. That
separation is what lets the pitch say "the live batch produced 0 false
reconciliations; here's the guard proving it would reject one if it appeared."
