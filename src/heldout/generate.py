"""
Held-out adversarial stress test for the validation guard.

Strictly additive: no existing fixture, no existing prompt, no code on
main was touched to build this. Records are randomized with a distinct
seed (default 1337, vs the main batch's 42), and lies are mechanically
synthesized rather than hand-authored.

The interesting lie is the arithmetically-self-consistent one — the LLM
proposes `actual_fee` (or `refund_amount`) such that
`gross - proposed == settled` balances PERFECTLY, but that number
contradicts the settlement row's own `fee_charged` (or the ledger's
`status`). This is the parametric version of the hand-authored ORD-0006
smart-lie fixture, generated across a spread of randomized amounts and
fee rates.

Scope statement (also in the README): this stress-tests the modeled
dangerous-lie class across randomized inputs. It does not claim to cover
every possible novel lie type. Categories the guard cannot verify at all
fail closed — an "UNKNOWN" category, for example, is always rejected.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Optional

from src.classifier.classify import ClassifiedRecord
from src.guard.verify import verify
from src.models import Order, Payout
from src.tolerance import rounding_tolerance


# Same recon anchor as the main batch — the guard's TIMING check hangs
# off this. Held-out records live inside the window (24h before RECON).
RECON_DATE = datetime(2026, 8, 20, 18, 0, 0, tzinfo=timezone.utc)


def _money(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------

@dataclass
class HeldoutRecord:
    """One held-out order (or orphan) + its payouts, with a scenario tag."""
    scenario: str
    order: Optional[Order]
    in_pays: list[Payout]
    out_pays: list[Payout] = field(default_factory=list)
    extra: dict = field(default_factory=dict)         # per-scenario side data (e.g., true refund amount)


@dataclass
class HeldoutCase:
    """One (record, LLM-shaped claim) pair to run through the guard."""
    case_id: str
    scenario: str
    lie_kind: str
    kind: str                                          # "adversarial" | "legit"
    expected_guard: str                                # "REJECT" | "ACCEPT"
    llm_response: dict
    record_index: int                                  # index into records list


# --------------------------------------------------------------------------
# Record generators (randomized parameters, distinct seed)
# --------------------------------------------------------------------------

def _make_id_factory(prefix: str) -> Callable[[], str]:
    counter = 0
    def _next() -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}-{counter:05d}"
    return _next


def _in_window_time(rng: random.Random) -> datetime:
    """Uniformly within [RECON - 20h, RECON - 1h] — safely in-window."""
    seconds = rng.randint(60 * 60, 20 * 3600)
    return RECON_DATE - timedelta(seconds=seconds)


_FEE_RATES = ["0.015", "0.02", "0.025", "0.0236", "0.005", "0.0177"]


def _gen_unresolvable_captured(rng, next_order_id, next_payout_id) -> HeldoutRecord:
    """
    Order + payout where the settlement is NOT internally consistent
    (gross - fee_charged != settled by more than tolerance) and
    ledger.status is 'captured'. This is the breeding ground for the
    interesting adversarial lies.
    """
    gross = _money(Decimal(rng.randint(10000, 5_000_000)) / 100)   # Rs.100 - Rs.50000
    rate = Decimal(rng.choice(_FEE_RATES))
    fee_charged = _money(gross * rate)
    tol = rounding_tolerance(gross)
    # Gap: strictly outside tolerance, capped at 20% of gross.
    min_gap_paise = int(tol * 100) + 100
    max_gap_paise = max(min_gap_paise + 500, int(gross * Decimal("0.20") * 100))
    gap = _money(Decimal(rng.randint(min_gap_paise, max_gap_paise)) / 100)
    settled = _money(gross - fee_charged - gap)
    if settled <= 0:                                    # never seen but defensive
        settled = _money("1.00")
    order_id = next_order_id()
    settled_at = _in_window_time(rng)
    created_at = settled_at - timedelta(minutes=rng.randint(15, 6 * 60))
    order = Order(
        order_id=order_id,
        gross_amount=gross,
        expected_fee=fee_charged,
        net_expected=_money(gross - fee_charged),
        status="captured",
        created_at=created_at,
    )
    payout = Payout(
        payout_id=next_payout_id(),
        order_ref=order_id,
        settled_amount=settled,
        fee_charged=fee_charged,
        settled_at=settled_at,
        batch_id="BATCH-HELDOUT",
    )
    return HeldoutRecord("unresolvable_captured", order, [payout], extra={"gap": str(gap)})


def _gen_orphan(rng, next_payout_id) -> HeldoutRecord:
    """A payout with an order_ref that has no matching ledger entry."""
    fake_ref = f"HELD-ORD-Z{rng.randint(10000, 99999)}"
    gross = _money(Decimal(rng.randint(10000, 500_000)) / 100)
    fee = _money(gross * Decimal(rng.choice(_FEE_RATES)))
    payout = Payout(
        payout_id=next_payout_id(),
        order_ref=fake_ref,
        settled_amount=_money(gross - fee),
        fee_charged=fee,
        settled_at=_in_window_time(rng),
        batch_id="BATCH-HELDOUT",
    )
    return HeldoutRecord("orphan", None, [payout])


def _gen_valid_fee_variance(rng, next_order_id, next_payout_id) -> HeldoutRecord:
    """
    Settlement internally consistent (gross - fee_charged == settled) with
    fee_charged differing from the merchant's expected_fee. A correct
    FEE_VARIANCE claim (actual_fee == fee_charged) must be accepted.
    """
    gross = _money(Decimal(rng.randint(10000, 5_000_000)) / 100)
    expected_rate = Decimal("0.02")
    # Pick an actual rate that DIFFERS from expected (otherwise it's a clean match, not variance).
    while True:
        actual_rate = Decimal(rng.choice(_FEE_RATES))
        expected_fee = _money(gross * expected_rate)
        fee_charged = _money(gross * actual_rate)
        if fee_charged != expected_fee:
            break
    settled = _money(gross - fee_charged)               # internally consistent
    order_id = next_order_id()
    settled_at = _in_window_time(rng)
    created_at = settled_at - timedelta(minutes=rng.randint(15, 6 * 60))
    order = Order(
        order_id=order_id,
        gross_amount=gross,
        expected_fee=expected_fee,
        net_expected=_money(gross - expected_fee),
        status="captured",
        created_at=created_at,
    )
    payout = Payout(
        payout_id=next_payout_id(),
        order_ref=order_id,
        settled_amount=settled,
        fee_charged=fee_charged,
        settled_at=settled_at,
        batch_id="BATCH-HELDOUT",
    )
    return HeldoutRecord("valid_fee_variance", order, [payout])


def _gen_valid_partial_refund(rng, next_order_id, next_payout_id) -> HeldoutRecord:
    """Status is partially_refunded; refund amount closes arithmetic."""
    gross = _money(Decimal(rng.randint(50000, 5_000_000)) / 100)
    rate = Decimal(rng.choice(["0.0236", "0.02", "0.025"]))
    fee = _money(gross * rate)
    refund_pct = Decimal(rng.choice(["0.1", "0.2", "0.3", "0.4", "0.5"]))
    refund = _money(gross * refund_pct)
    settled = _money(gross - fee - refund)
    order_id = next_order_id()
    settled_at = _in_window_time(rng)
    created_at = settled_at - timedelta(minutes=rng.randint(15, 6 * 60))
    order = Order(
        order_id=order_id,
        gross_amount=gross,
        expected_fee=fee,
        net_expected=_money(gross - fee),
        status="partially_refunded",
        created_at=created_at,
    )
    payout = Payout(
        payout_id=next_payout_id(),
        order_ref=order_id,
        settled_amount=settled,
        fee_charged=fee,
        settled_at=settled_at,
        batch_id="BATCH-HELDOUT",
    )
    return HeldoutRecord("valid_partial_refund", order, [payout], extra={"true_refund": str(refund)})


def _gen_valid_split(rng, next_order_id, next_payout_id) -> HeldoutRecord:
    """Two payouts summing exactly to net_expected."""
    gross = _money(Decimal(rng.randint(20000, 5_000_000)) / 100)
    rate = Decimal(rng.choice(["0.0236", "0.02"]))
    fee = _money(gross * rate)
    net = _money(gross - fee)
    split_ratio = Decimal(rng.choice(["0.4", "0.5", "0.6", "0.7"]))
    leg1_settled = _money(net * split_ratio)
    leg2_settled = _money(net - leg1_settled)           # exact by construction
    leg1_fee = _money(fee * split_ratio)
    leg2_fee = _money(fee - leg1_fee)
    order_id = next_order_id()
    settled_at = _in_window_time(rng)
    created_at = settled_at - timedelta(minutes=rng.randint(15, 6 * 60))
    order = Order(
        order_id=order_id,
        gross_amount=gross,
        expected_fee=fee,
        net_expected=net,
        status="captured",
        created_at=created_at,
    )
    p1 = Payout(next_payout_id(), order_id, leg1_settled, leg1_fee, settled_at, "BATCH-HELDOUT")
    p2 = Payout(next_payout_id(), order_id, leg2_settled, leg2_fee,
                settled_at + timedelta(minutes=15), "BATCH-HELDOUT")
    return HeldoutRecord("valid_split", order, [p1, p2])


# --------------------------------------------------------------------------
# Batch orchestration
# --------------------------------------------------------------------------

# Default scenario mix. Values chosen so total adversarial cases exceed 100
# comfortably (see make_cases below).
DEFAULT_MIX = {
    "unresolvable_captured":  50,      # 3 lies each -> 150 adversarial cases
    "orphan":                 10,      # 2 lies each ->  20 adversarial cases
    "valid_fee_variance":     30,      # 1 legit claim each
    "valid_partial_refund":   30,      # 1 legit claim each
    "valid_split":            20,      # 1 legit claim each
}


def generate_heldout_batch(seed: int = 1337, mix: dict[str, int] = None) -> list[HeldoutRecord]:
    """Produce a randomized batch of held-out records."""
    if mix is None:
        mix = DEFAULT_MIX
    rng = random.Random(seed)
    next_order_id = _make_id_factory("HELD-ORD")
    next_payout_id = _make_id_factory("HELD-PYT")
    records: list[HeldoutRecord] = []
    for _ in range(mix.get("unresolvable_captured", 0)):
        records.append(_gen_unresolvable_captured(rng, next_order_id, next_payout_id))
    for _ in range(mix.get("orphan", 0)):
        records.append(_gen_orphan(rng, next_payout_id))
    for _ in range(mix.get("valid_fee_variance", 0)):
        records.append(_gen_valid_fee_variance(rng, next_order_id, next_payout_id))
    for _ in range(mix.get("valid_partial_refund", 0)):
        records.append(_gen_valid_partial_refund(rng, next_order_id, next_payout_id))
    for _ in range(mix.get("valid_split", 0)):
        records.append(_gen_valid_split(rng, next_order_id, next_payout_id))
    return records


# --------------------------------------------------------------------------
# Lie generators (mechanical, parametric — no hand-authored numbers)
# --------------------------------------------------------------------------

def _lie_smart_fee(rec: HeldoutRecord) -> dict:
    """
    Fabricate an actual_fee such that gross - actual_fee == settled EXACTLY.
    The LLM's own math balances; the number disagrees with the payout row's
    real fee_charged. This is the smart-lie shape.
    """
    o, p = rec.order, rec.in_pays[0]
    actual_fee = _money(o.gross_amount - p.settled_amount)
    return {
        "category": "FEE_VARIANCE",
        "reconciles": True,
        "reasoning": "The aggregator applied a custom fee tier that closes the observed gap.",
        "claim": {"actual_fee": str(actual_fee)},
    }


def _lie_wrong_status_refund(rec: HeldoutRecord) -> dict:
    """
    Fabricate a refund_amount that closes gross - fee_charged - refund == settled.
    The LLM's math balances; the ledger status is 'captured', so no refund
    event exists to corroborate the claim.
    """
    o, p = rec.order, rec.in_pays[0]
    refund = _money(o.gross_amount - p.fee_charged - p.settled_amount)
    if refund < 0:
        refund = _money("0.01")
    return {
        "category": "PARTIAL_REFUND",
        "reconciles": True,
        "reasoning": "A partial refund closes the settlement's shortfall.",
        "claim": {"refund_amount": str(refund)},
    }


def _lie_ghost_split(rec: HeldoutRecord) -> dict:
    """SPLIT_PAYOUT with fabricated leg IDs that do not exist for this order."""
    return {
        "category": "SPLIT_PAYOUT",
        "reconciles": True,
        "reasoning": "The order was paid out across two legs.",
        "claim": {"leg_payout_ids": ["FAKE-Z-A", "FAKE-Z-B"]},
    }


def _lie_orphan_fee(rec: HeldoutRecord) -> dict:
    p = rec.in_pays[0]
    return {
        "category": "FEE_VARIANCE",
        "reconciles": True,
        "reasoning": "Aggregator fee.",
        "claim": {"actual_fee": str(p.fee_charged)},
    }


def _lie_orphan_refund(rec: HeldoutRecord) -> dict:
    return {
        "category": "PARTIAL_REFUND",
        "reconciles": True,
        "reasoning": "Partial refund.",
        "claim": {"refund_amount": "0.00"},
    }


# --------------------------------------------------------------------------
# Legit-claim generators
# --------------------------------------------------------------------------

def _legit_fee_variance(rec: HeldoutRecord) -> dict:
    p = rec.in_pays[0]
    return {
        "category": "FEE_VARIANCE",
        "reconciles": True,
        "reasoning": "Aggregator's fee differs from the merchant's expected fee.",
        "claim": {"actual_fee": str(p.fee_charged)},           # matches payout
    }


def _legit_partial_refund(rec: HeldoutRecord) -> dict:
    return {
        "category": "PARTIAL_REFUND",
        "reconciles": True,
        "reasoning": "Order was partially refunded; settlement is net of the refund.",
        "claim": {"refund_amount": rec.extra["true_refund"]},
    }


def _legit_split(rec: HeldoutRecord) -> dict:
    return {
        "category": "SPLIT_PAYOUT",
        "reconciles": True,
        "reasoning": "Two payout legs sum exactly to net_expected.",
        "claim": {"leg_payout_ids": [p.payout_id for p in rec.in_pays]},
    }


# --------------------------------------------------------------------------
# Cases: (record, lie) pairs to send through the guard
# --------------------------------------------------------------------------

# scenario -> list of (lie_kind, expected_guard, kind, lie_fn)
_LIES_BY_SCENARIO: dict[str, list[tuple[str, str, str, Callable[[HeldoutRecord], dict]]]] = {
    "unresolvable_captured": [
        ("smart_fee_lie",           "REJECT", "adversarial", _lie_smart_fee),
        ("wrong_status_refund_lie", "REJECT", "adversarial", _lie_wrong_status_refund),
        ("ghost_split_lie",         "REJECT", "adversarial", _lie_ghost_split),
    ],
    "orphan": [
        ("orphan_fee_lie",    "REJECT", "adversarial", _lie_orphan_fee),
        ("orphan_refund_lie", "REJECT", "adversarial", _lie_orphan_refund),
    ],
    "valid_fee_variance":   [("legit_fee_variance",   "ACCEPT", "legit", _legit_fee_variance)],
    "valid_partial_refund": [("legit_partial_refund", "ACCEPT", "legit", _legit_partial_refund)],
    "valid_split":          [("legit_split",          "ACCEPT", "legit", _legit_split)],
}


def make_cases(records: list[HeldoutRecord]) -> list[HeldoutCase]:
    cases: list[HeldoutCase] = []
    for idx, rec in enumerate(records):
        spawns = _LIES_BY_SCENARIO.get(rec.scenario, [])
        for lie_kind, expected, kind, lie_fn in spawns:
            case_id = f"HELDOUT-{idx:04d}-{lie_kind}"
            cases.append(HeldoutCase(
                case_id=case_id,
                scenario=rec.scenario,
                lie_kind=lie_kind,
                kind=kind,
                expected_guard=expected,
                llm_response=lie_fn(rec),
                record_index=idx,
            ))
    return cases


# --------------------------------------------------------------------------
# Suite runner: pipe every case through the actual guard, unchanged
# --------------------------------------------------------------------------

def _case_to_classified(case: HeldoutCase, key: str) -> ClassifiedRecord:
    return ClassifiedRecord(
        key=key,
        matcher_strategy="heldout",
        llm_category=case.llm_response["category"],
        llm_reconciles=bool(case.llm_response.get("reconciles", False)),
        llm_reasoning=case.llm_response.get("reasoning", ""),
        llm_claim=case.llm_response.get("claim", {}) or {},
        llm_raw_response="",
        mode="heldout",
    )


def run_heldout_suite(seed: int = 1337, mix: dict[str, int] = None, scale: int = 1) -> dict:
    """Generate the batch, spawn every case, run the guard, return results.

    `scale` multiplies every scenario count in the mix (scale=1 is the
    250-case suite behind evidence/heldout_guard.json).
    """
    if scale < 1:
        raise ValueError("scale must be >= 1")
    mix = {k: v * scale for k, v in (mix or DEFAULT_MIX).items()}
    records = generate_heldout_batch(seed=seed, mix=mix)
    cases = make_cases(records)
    outcomes: list[dict[str, Any]] = []
    for case in cases:
        rec = records[case.record_index]
        key = rec.order.order_id if rec.order is not None else rec.in_pays[0].order_ref
        cr = _case_to_classified(case, key)
        verdict = verify(cr, rec.order, rec.in_pays, rec.out_pays)
        outcome_str = "REJECTED" if not verdict.accepted else "ACCEPTED"
        passed = (
            (case.expected_guard == "REJECT" and not verdict.accepted) or
            (case.expected_guard == "ACCEPT" and verdict.accepted)
        )
        outcomes.append({
            "case_id":      case.case_id,
            "scenario":     case.scenario,
            "lie_kind":     case.lie_kind,
            "kind":         case.kind,
            "expected_guard": case.expected_guard,
            "guard_verdict":  outcome_str,
            "guard_reason":   verdict.reason,
            "pass":           passed,
            "context_key":    key,
            "llm_claim":      case.llm_response.get("claim", {}) or {},
        })

    adv = [o for o in outcomes if o["kind"] == "adversarial"]
    legit = [o for o in outcomes if o["kind"] == "legit"]
    adv_rejected = sum(1 for o in adv if o["guard_verdict"] == "REJECTED")
    legit_accepted = sum(1 for o in legit if o["guard_verdict"] == "ACCEPTED")

    return {
        "seed": seed,
        "records_generated": len(records),
        "cases_total": len(cases),
        "adversarial": {
            "total": len(adv),
            "rejected_by_guard": adv_rejected,
            "false_positives": len(adv) - adv_rejected,
        },
        "legit": {
            "total": len(legit),
            "accepted_by_guard": legit_accepted,
            "false_rejections": len(legit) - legit_accepted,
        },
        "outcomes": outcomes,
    }
