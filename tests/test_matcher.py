"""
Unit tests for the deterministic matcher.

Invariant enforced here: the matcher's implementation must not import
`src.generator` and must not read `ground_truth.json`. Tests set up
Orders and Payouts directly in memory. The one end-to-end test uses the
generator's SCENARIO_MIX (a source constant, not a labels file) to derive
the expected match count, so we measure honestly instead of tuning to
labels.
"""
from __future__ import annotations

import importlib
import sys
from datetime import timedelta
from decimal import Decimal

import pytest

from src.matcher.match import (
    DEFAULT_RECON_DATE,
    DEFAULT_WINDOW,
    load_orders,
    load_payouts,
    match_batch,
)
from src.models import Order, Payout
from src.tolerance import rounding_tolerance


RECON = DEFAULT_RECON_DATE
WINDOW = DEFAULT_WINDOW
IN_WINDOW = RECON - timedelta(hours=2)
OUT_OF_WINDOW = RECON + timedelta(hours=3)


# --- test fixtures --------------------------------------------------------

def make_order(order_id="ORD-1", gross="1000.00", fee="23.60", status="captured"):
    gross_d = Decimal(gross)
    fee_d = Decimal(fee)
    return Order(
        order_id=order_id,
        gross_amount=gross_d,
        expected_fee=fee_d,
        net_expected=gross_d - fee_d,
        status=status,
        created_at=IN_WINDOW - timedelta(hours=1),
    )


def make_payout(payout_id, order_ref, settled, fee, at=IN_WINDOW):
    return Payout(
        payout_id=payout_id,
        order_ref=order_ref,
        settled_amount=Decimal(settled),
        fee_charged=Decimal(fee),
        settled_at=at,
        batch_id="BATCH-TEST",
    )


def _one(decisions):
    assert len(decisions) == 1
    return decisions[0]


# --- 1. exact match -------------------------------------------------------

def test_exact_match_single_payout():
    o = make_order()                       # net = 976.40
    p = make_payout("PYT-1", "ORD-1", "976.40", "23.60")
    d = _one(match_batch([o], [p]))
    assert d.status == "MATCHED"
    assert d.strategy == "exact"
    assert d.matched_payout_ids == ["PYT-1"]
    assert d.tolerance_gap == Decimal("0")
    assert d.tolerance_used == rounding_tolerance(o.gross_amount)
    assert d.out_of_window_payout_ids == []


# --- 2. within-tolerance boundary ----------------------------------------

def test_within_tolerance_at_boundary():
    o = make_order()                       # tol = max(0.50, 1.00) = 1.00
    p = make_payout("PYT-1", "ORD-1", "977.40", "23.60")   # gap +1.00
    d = _one(match_batch([o], [p]))
    assert d.status == "MATCHED"
    assert d.strategy == "within_tolerance"
    assert d.tolerance_gap == Decimal("1.00")
    assert d.tolerance_used == Decimal("1.00")
    assert "absorbed gap" in d.reason.lower()
    assert "ceiling" in d.reason.lower()


def test_within_tolerance_just_beyond_boundary():
    o = make_order()                       # tol = 1.00
    p = make_payout("PYT-1", "ORD-1", "977.41", "23.60")   # gap 1.01 > tol
    d = _one(match_batch([o], [p]))
    assert d.status == "UNMATCHED_ESCALATE"
    assert d.strategy == "amount_mismatch"
    assert d.tolerance_gap == Decimal("1.01")
    assert d.tolerance_used == Decimal("1.00")


# --- 3. split-payout sum -------------------------------------------------

def test_split_payout_sum_exact():
    o = make_order()                       # net = 976.40
    p1 = make_payout("PYT-1", "ORD-1", "585.84", "14.16")
    p2 = make_payout("PYT-2", "ORD-1", "390.56", "9.44")
    d = _one(match_batch([o], [p1, p2]))
    assert d.status == "MATCHED"
    assert d.strategy == "split_sum"
    assert sorted(d.matched_payout_ids) == ["PYT-1", "PYT-2"]
    assert d.tolerance_gap == Decimal("0")


def test_split_payout_sum_within_tolerance():
    o = make_order()                       # tol = 1.00
    p1 = make_payout("PYT-1", "ORD-1", "585.84", "14.16")
    p2 = make_payout("PYT-2", "ORD-1", "391.06", "9.44")   # +0.50 drift
    d = _one(match_batch([o], [p1, p2]))
    assert d.status == "MATCHED"
    assert d.strategy == "split_sum"
    assert d.tolerance_gap == Decimal("0.50")


# --- 5. split incomplete: all in-window, sums don't reconcile ------------

def test_split_incomplete_sum_mismatch():
    o = make_order(gross="6300.00", fee="148.68")          # net 6151.32
    p1 = make_payout("PYT-1", "ORD-1", "3500.00", "80.00")
    p2 = make_payout("PYT-2", "ORD-1", "2400.00", "100.00")
    d = _one(match_batch([o], [p1, p2]))
    assert d.status == "UNMATCHED_ESCALATE"
    assert d.strategy == "split_incomplete"
    assert d.reason.startswith("[sum_mismatch]")
    assert sorted(d.matched_payout_ids) == ["PYT-1", "PYT-2"]
    assert d.out_of_window_payout_ids == []
    assert d.tolerance_gap == Decimal("251.32")


# --- 6. split incomplete: one leg out-of-window (late_leg precedence) ---

def test_split_incomplete_late_leg_wins_over_sum_mismatch():
    """
    Precedence check: even though the in-window legs alone don't sum,
    the presence of an out-of-window leg makes this a `late_leg` case,
    not a `sum_mismatch`. Missing data beats inconsistent data.
    """
    o = make_order(gross="6300.00", fee="148.68")
    p1 = make_payout("PYT-1", "ORD-1", "3500.00", "80.00", at=IN_WINDOW)
    p2 = make_payout("PYT-2", "ORD-1", "2651.32", "68.68", at=OUT_OF_WINDOW)
    d = _one(match_batch([o], [p1, p2]))
    assert d.status == "UNMATCHED_ESCALATE"
    assert d.strategy == "split_incomplete"
    assert d.reason.startswith("[late_leg]")
    assert d.matched_payout_ids == ["PYT-1"]
    assert d.out_of_window_payout_ids == ["PYT-2"]


# --- 7. timing: single payout out-of-window -----------------------------

def test_all_payouts_out_of_window():
    o = make_order()
    p = make_payout("PYT-1", "ORD-1", "976.40", "23.60", at=OUT_OF_WINDOW)
    d = _one(match_batch([o], [p]))
    assert d.status == "UNMATCHED_ESCALATE"
    assert d.strategy == "all_payouts_out_of_window"
    assert d.out_of_window_payout_ids == ["PYT-1"]
    assert d.matched_payout_ids == []
    assert d.tolerance_used is None      # we never ran the tolerance check


# --- 8. no payouts found -------------------------------------------------

def test_no_payouts_found():
    o = make_order()
    d = _one(match_batch([o], []))
    assert d.status == "UNMATCHED_ESCALATE"
    assert d.strategy == "no_payouts_found"
    assert d.matched_payout_ids == []
    assert d.out_of_window_payout_ids == []


# --- 9. amount mismatch — near-miss shape from the generator -----------

def test_amount_mismatch_near_miss_from_generator_spec():
    """
    Exactly the ORD-0006 shape produced by the generator: gross 4823,
    fee 96.46, settled 4703.20 -> gap 23.34. Tolerance for gross 4823
    is max(0.50, 4.823 -> 4.82) = 4.82. This escalates cleanly; the
    validation guard (step 4) will later reject any LLM story that
    claims this reconciles.
    """
    o = make_order(order_id="ORD-6", gross="4823.00", fee="96.46")
    p = make_payout("PYT-1", "ORD-6", "4703.20", "96.46")
    d = _one(match_batch([o], [p]))
    assert d.status == "UNMATCHED_ESCALATE"
    assert d.strategy == "amount_mismatch"
    assert d.tolerance_gap == Decimal("23.34")
    assert d.tolerance_used == Decimal("4.82")


# --- 10. orphan payout ---------------------------------------------------

def test_orphan_payout_no_matching_order():
    p = make_payout("PYT-99", "ORD-9999", "1000.00", "20.00")
    decisions = match_batch([], [p])
    assert len(decisions) == 1
    d = decisions[0]
    assert d.status == "UNMATCHED_ESCALATE"
    assert d.strategy == "orphan_payout"
    assert d.key == "ORD-9999"
    assert d.matched_payout_ids == ["PYT-99"]
    assert d.tolerance_used is None


# --- Invariant: matcher does not read ground truth or import generator --

def test_matcher_module_does_not_import_generator():
    """
    Hard architectural check. If someone accidentally couples the matcher
    to the generator (a code path that could also touch ground_truth), this
    test fails loudly. We check the true invariant: importing the matcher
    must not drag in the generator as a transitive dependency.

    We first purge every `src.*` module from sys.modules so this test is
    not sensitive to whichever test ran before us (some tests do import
    the generator legitimately).
    """
    for mod_name in [m for m in sys.modules if m.startswith("src.")]:
        sys.modules.pop(mod_name, None)
    importlib.import_module("src.matcher.match")
    assert "src.generator" not in sys.modules, (
        "src.matcher.match imported src.generator as a side effect — hard invariant violated."
    )
    assert "src.generator.generate" not in sys.modules


# --- 11. end-to-end against generator output — measure, do not tune ----

def test_end_to_end_matches_deterministic_ceiling(tmp_path):
    """
    Run the generator into a temp dir, then run the matcher. Assert
    counts derived from SCENARIO_MIX (a source-code constant), not from
    ground_truth.json. If the honest logic returns 50 or 52 instead of
    51, that is the number we report — this test is a change-detector,
    not a target to tune toward.
    """
    from src.generator.generate import SCENARIO_MIX, generate

    generate(seed=42, out_dir=tmp_path)
    orders = load_orders(tmp_path / "orders_ledger.csv")
    payouts = load_payouts(tmp_path / "settlement_file.csv")
    decisions = match_batch(orders, payouts)

    expected_matched = (
        SCENARIO_MIX["clean"]
        + SCENARIO_MIX["rounding"]
        + SCENARIO_MIX["split_payout_clean"]
    )
    expected_escalated = (
        SCENARIO_MIX["fee_variance"]
        + SCENARIO_MIX["partial_refund"]
        + SCENARIO_MIX["timing_window"]
        + SCENARIO_MIX["genuine_mismatch_near_miss"]
        + SCENARIO_MIX["split_payout_messy"]
        + SCENARIO_MIX["genuine_mismatch_orphan"]      # orphan is a payout-side decision
    )

    matched = sum(1 for d in decisions if d.status == "MATCHED")
    escalated = sum(1 for d in decisions if d.status == "UNMATCHED_ESCALATE")

    assert matched == expected_matched, (
        f"Deterministic matcher produced {matched} MATCHED; expected "
        f"{expected_matched} (45 clean + 3 rounding + 3 split_clean). "
        f"Don't tune the matcher to hit this number — investigate the drift."
    )
    assert escalated == expected_escalated
    assert len(decisions) == expected_matched + expected_escalated

    # Timing scenario -> exactly one all_payouts_out_of_window decision.
    strategies = [d.strategy for d in decisions]
    assert strategies.count("all_payouts_out_of_window") == SCENARIO_MIX["timing_window"]
    # Messy split -> exactly one split_incomplete decision.
    assert strategies.count("split_incomplete") == SCENARIO_MIX["split_payout_messy"]
    # Orphan -> exactly one.
    assert strategies.count("orphan_payout") == SCENARIO_MIX["genuine_mismatch_orphan"]


# --- fee variance hidden inside the rounding tolerance --------------------

def test_small_fee_variance_inside_tolerance_is_escalated_not_absorbed():
    # Real case from the 100,800-record scale run: a ₹138.13 card order where
    # the merchant expected a flat 2% fee (₹2.76) and the aggregator charged
    # 2% + GST (₹3.26). The ₹0.50 gap fits under the ₹0.50 rounding floor,
    # but rounding never changes the fee, so this must not auto-match.
    o = make_order("ORD-0409", gross="138.13", fee="2.76")
    p = make_payout("PYT-0438", "ORD-0409", settled="134.87", fee="3.26")
    [d] = match_batch([o], [p], recon_date=RECON, window=WINDOW)
    assert d.status == "UNMATCHED_ESCALATE"
    assert d.strategy == "fee_mismatch"
    assert "fee_charged Rs.3.26" in d.reason


def test_rounding_drift_with_unchanged_fee_still_matches():
    # The control: same size of gap, but the fee is what the merchant expected.
    o = make_order("ORD-1", gross="138.13", fee="2.76")
    p = make_payout("PYT-1", "ORD-1", settled="134.87", fee="2.76")
    [d] = match_batch([o], [p], recon_date=RECON, window=WINDOW)
    assert d.status == "MATCHED"
    assert d.strategy == "within_tolerance"
