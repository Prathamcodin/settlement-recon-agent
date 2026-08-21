"""
Validation guard — layer 3 of the reconciliation pipeline.

The safety layer. Every LLM classification is re-checked against hard
arithmetic and ledger evidence before it is accepted. If any invariant
fails, the classification is REJECTED and the record is forced to
GENUINE_MISMATCH so it lands in the human-review queue instead of the
auto-reconciled bucket.

The guard is the reason the LLM can be treated as bounded and untrusted.
It never trusts the LLM's own numbers over the raw settlement data —
that is the whole point of the "smart lie" case: a model can propose
values that arithmetically balance, but the guard validates against the
fields already recorded in the payout row, not against numbers the
model produced.

Design notes:
  - Reads the SAME rounding_tolerance module as the matcher, so the
    tolerance the matcher applied is exactly the tolerance the guard
    applies. Divergence here would be its own bug.
  - GENUINE_MISMATCH is always ACCEPTED (the LLM saying "I cannot
    classify this" is an honest, non-dangerous verdict).
  - reconciles=false is ACCEPTED as GENUINE_MISMATCH — the LLM has
    voluntarily escalated. But the guard STILL runs the category's
    arithmetic checks and logs whether they would have passed, so the
    audit trail never blindly trusts the reconciles flag.
  - Every check is logged in the verdict's `checks` list with a
    pass/fail flag and detail string, whether it succeeded or failed.
    The `reason` field is a summary intended for humans; the `checks`
    field is the machine-readable audit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from src.classifier.classify import ClassifiedRecord
from src.matcher.match import DEFAULT_RECON_DATE, DEFAULT_WINDOW
from src.models import MatchDecision, Order, Payout
from src.tolerance import rounding_tolerance


# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------

@dataclass
class Check:
    """One named invariant, with its outcome and a human-readable detail."""
    name: str
    passed: bool
    detail: str


@dataclass
class GuardVerdict:
    accepted: bool
    final_category: str                     # forced to GENUINE_MISMATCH on reject
    reason: str
    checks: list[Check] = field(default_factory=list)


# --------------------------------------------------------------------------
# Public entrypoint
# --------------------------------------------------------------------------

def verify(
    record: ClassifiedRecord,
    order: Optional[Order],
    in_pays: list[Payout],
    out_pays: list[Payout],
    *,
    recon_date: datetime = DEFAULT_RECON_DATE,
    window: timedelta = DEFAULT_WINDOW,
) -> GuardVerdict:
    cat = record.llm_category

    # LLM said "I cannot classify" — honest verdict, no claim to verify.
    if cat == "GENUINE_MISMATCH":
        return GuardVerdict(
            accepted=True,
            final_category="GENUINE_MISMATCH",
            reason="LLM classified as GENUINE_MISMATCH; no claim to verify.",
            checks=[Check("llm_declined_to_classify", True,
                          "reconciles=False, category=GENUINE_MISMATCH")],
        )

    # Dispatch to category-specific checks. The check function returns
    # a full GuardVerdict as if reconciles=true; we honor or downgrade it below.
    intermediate = _dispatch(record, order, in_pays, out_pays, recon_date, window)

    if record.llm_reconciles:
        # LLM asked us to auto-clear; honor the check outcome exactly.
        return intermediate

    # LLM proposed a category but declined to auto-reconcile. We escalate as
    # GENUINE_MISMATCH regardless — but run the checks so the audit trail
    # shows whether the LLM's proposal would have withstood scrutiny. This
    # is the "no silent bypass" invariant: the guard never trusts the
    # reconciles flag alone.
    counterfactual = "would have PASSED" if intermediate.accepted else "would have FAILED"
    return GuardVerdict(
        accepted=True,
        final_category="GENUINE_MISMATCH",
        reason=(
            f"LLM proposed {cat} but reconciles=false; escalated as exception. "
            f"Guard also ran the {cat} checks: they {counterfactual}."
        ),
        checks=intermediate.checks,
    )


def _dispatch(record, order, in_pays, out_pays, recon_date, window) -> GuardVerdict:
    cat = record.llm_category
    if cat == "FEE_VARIANCE":
        return _check_fee_variance(record, order, in_pays)
    if cat == "PARTIAL_REFUND":
        return _check_partial_refund(record, order, in_pays)
    if cat == "SPLIT_PAYOUT":
        return _check_split(record, order, in_pays, allow_fee_variance=False)
    if cat == "SPLIT_PAYOUT_WITH_VARIANCE":
        return _check_split(record, order, in_pays, allow_fee_variance=True)
    if cat == "TIMING_WINDOW":
        return _check_timing(record, order, in_pays, out_pays, recon_date, window)
    return GuardVerdict(
        accepted=False,
        final_category="GENUINE_MISMATCH",
        reason=f"Unknown LLM category '{cat}'; rejected as unrecognized.",
        checks=[Check("known_category", False, f"category '{cat}' not in schema")],
    )


# --------------------------------------------------------------------------
# Category-specific checks. Each runs every relevant invariant, populates
# a checks list, and only returns REJECTED if any check failed.
# --------------------------------------------------------------------------

def _decimal(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _finalize(category: str, checks: list[Check], reject_reason_builder=None) -> GuardVerdict:
    failed = [c for c in checks if not c.passed]
    if not failed:
        return GuardVerdict(
            accepted=True,
            final_category=category,
            reason=f"{category} verified: {len(checks)} invariant(s) passed.",
            checks=checks,
        )
    if reject_reason_builder is not None:
        reason = reject_reason_builder(failed)
    else:
        parts = [f"{c.name} ({c.detail})" for c in failed]
        reason = f"{category} rejected. Failures: {' | '.join(parts)}"
    return GuardVerdict(
        accepted=False,
        final_category="GENUINE_MISMATCH",
        reason=reason,
        checks=checks,
    )


def _check_fee_variance(record, order, in_pays) -> GuardVerdict:
    checks: list[Check] = []
    if order is None:
        checks.append(Check("order_exists", False, "no ledger order present (orphan payout)"))
        return _finalize("FEE_VARIANCE", checks)
    checks.append(Check("order_exists", True, f"order {order.order_id}"))

    if len(in_pays) != 1:
        checks.append(Check("payout_count", False,
                            f"{len(in_pays)} in-window payouts (require exactly 1)"))
        return _finalize("FEE_VARIANCE", checks)
    checks.append(Check("payout_count", True, "exactly 1 in-window payout"))

    p = in_pays[0]
    tol = rounding_tolerance(order.gross_amount)

    # Invariant A: settlement is internally consistent — the payout's OWN
    # fee_charged field must reconcile gross to settled within tolerance.
    internal_lhs = order.gross_amount - p.fee_charged
    internal_gap = abs(internal_lhs - p.settled_amount)
    checks.append(Check(
        name="settlement_internally_consistent",
        passed=internal_gap <= tol,
        detail=(
            f"gross Rs.{order.gross_amount} - fee_charged Rs.{p.fee_charged} = "
            f"Rs.{internal_lhs}; settled Rs.{p.settled_amount}; "
            f"gap Rs.{internal_gap}; tolerance Rs.{tol}"
        ),
    ))

    # Invariant B: if the LLM proposed an actual_fee, it must agree with
    # the payout's fee_charged. Without this check, a self-consistent
    # fabricated actual_fee (the "smart lie") would slip through — the
    # model can trivially make its own arithmetic balance, but the
    # settlement's actual fee is what we reconcile against.
    claim_fee_str = record.llm_claim.get("actual_fee")
    if claim_fee_str is None:
        checks.append(Check("llm_actual_fee_provided", True,
                            "no actual_fee provided in claim (skipping cross-check)"))
    else:
        claim_fee = _decimal(claim_fee_str)
        if claim_fee is None:
            checks.append(Check(
                "llm_actual_fee_provided", False,
                f"claim.actual_fee '{claim_fee_str}' is not parseable as Decimal",
            ))
        else:
            checks.append(Check("llm_actual_fee_provided", True, f"claim.actual_fee = Rs.{claim_fee}"))
            fee_gap = abs(claim_fee - p.fee_charged)
            checks.append(Check(
                name="llm_actual_fee_agrees_with_payout",
                passed=fee_gap <= tol,
                detail=(
                    f"LLM's actual_fee Rs.{claim_fee} vs payout's fee_charged "
                    f"Rs.{p.fee_charged}; gap Rs.{fee_gap}; tolerance Rs.{tol}"
                ),
            ))

    def _reason(failed):
        # If the LLM's proposal is arithmetically self-consistent (its
        # own math balances) but it disagrees with the payout's
        # fee_charged, surface that as the primary reason — this is the
        # "smart lie" pattern and the pitch talking point.
        claim_fee = _decimal(record.llm_claim.get("actual_fee"))
        smart_lie = False
        if claim_fee is not None:
            llm_math_gap = abs((order.gross_amount - claim_fee) - p.settled_amount)
            llm_disagrees = abs(claim_fee - p.fee_charged) > tol
            smart_lie = llm_math_gap <= tol and llm_disagrees
        if smart_lie:
            return (
                f"FEE_VARIANCE rejected: the LLM's proposed actual_fee "
                f"Rs.{claim_fee} would arithmetically close the gap if used in "
                f"place of fee_charged, but it disagrees with the payout row's "
                f"actual fee_charged Rs.{p.fee_charged} by Rs.{abs(claim_fee - p.fee_charged)} "
                f"(> tolerance Rs.{tol}). The guard validates against the settlement's "
                f"recorded fee, not the model's proposal."
            )
        parts = [f"{c.name} ({c.detail})" for c in failed]
        return f"FEE_VARIANCE rejected. Failures: {' | '.join(parts)}"

    return _finalize("FEE_VARIANCE", checks, _reason)


def _check_partial_refund(record, order, in_pays) -> GuardVerdict:
    checks: list[Check] = []
    if order is None:
        checks.append(Check("order_exists", False, "no ledger order present"))
        return _finalize("PARTIAL_REFUND", checks)
    checks.append(Check("order_exists", True, f"order {order.order_id}"))

    checks.append(Check(
        name="ledger_status_supports_refund",
        passed=order.status in ("partially_refunded", "refunded"),
        detail=f"order.status = '{order.status}' (expected 'partially_refunded' or 'refunded')",
    ))

    if len(in_pays) != 1:
        checks.append(Check("payout_count", False,
                            f"{len(in_pays)} in-window payouts (require exactly 1)"))
        return _finalize("PARTIAL_REFUND", checks)
    checks.append(Check("payout_count", True, "exactly 1 in-window payout"))

    p = in_pays[0]
    tol = rounding_tolerance(order.gross_amount)
    claim_refund_str = record.llm_claim.get("refund_amount")
    if claim_refund_str is None:
        checks.append(Check("refund_amount_provided", False,
                            "claim.refund_amount is missing"))
        return _finalize("PARTIAL_REFUND", checks)
    refund = _decimal(claim_refund_str)
    if refund is None:
        checks.append(Check("refund_amount_provided", False,
                            f"claim.refund_amount '{claim_refund_str}' not parseable"))
        return _finalize("PARTIAL_REFUND", checks)
    checks.append(Check("refund_amount_provided", True, f"claim.refund_amount = Rs.{refund}"))
    checks.append(Check(
        name="refund_amount_non_negative",
        passed=refund >= Decimal("0"),
        detail=f"refund Rs.{refund}",
    ))

    lhs = order.gross_amount - p.fee_charged - refund
    arithmetic_gap = abs(lhs - p.settled_amount)
    checks.append(Check(
        name="refund_arithmetic_closes",
        passed=arithmetic_gap <= tol,
        detail=(
            f"gross Rs.{order.gross_amount} - fee_charged Rs.{p.fee_charged} - "
            f"refund Rs.{refund} = Rs.{lhs}; settled Rs.{p.settled_amount}; "
            f"gap Rs.{arithmetic_gap}; tolerance Rs.{tol}"
        ),
    ))
    return _finalize("PARTIAL_REFUND", checks)


def _check_split(record, order, in_pays, *, allow_fee_variance: bool) -> GuardVerdict:
    label = "SPLIT_PAYOUT_WITH_VARIANCE" if allow_fee_variance else "SPLIT_PAYOUT"
    checks: list[Check] = []
    if order is None:
        checks.append(Check("order_exists", False, "no ledger order present"))
        return _finalize(label, checks)
    checks.append(Check("order_exists", True, f"order {order.order_id}"))

    leg_ids = record.llm_claim.get("leg_payout_ids")
    if not isinstance(leg_ids, list) or len(leg_ids) < 2:
        checks.append(Check(
            name="leg_payout_ids_shape",
            passed=False,
            detail=f"claim.leg_payout_ids must be a list of >= 2 IDs; got {leg_ids!r}",
        ))
        return _finalize(label, checks)
    checks.append(Check("leg_payout_ids_shape", True, f"{len(leg_ids)} leg IDs proposed"))

    in_by_id = {p.payout_id: p for p in in_pays}
    missing = [pid for pid in leg_ids if pid not in in_by_id]
    checks.append(Check(
        name="all_legs_exist_and_in_window",
        passed=not missing,
        detail=(
            f"claimed leg IDs {leg_ids} vs in-window payouts {list(in_by_id.keys())}"
            + (f"; missing/unknown: {missing}" if missing else "")
        ),
    ))
    if missing:
        return _finalize(label, checks)

    legs = [in_by_id[pid] for pid in leg_ids]
    tol = rounding_tolerance(order.gross_amount)
    sum_settled = sum((leg.settled_amount for leg in legs), Decimal("0"))
    fee_variance = Decimal("0")
    if allow_fee_variance:
        fv = _decimal(record.llm_claim.get("fee_variance"))
        if fv is None:
            checks.append(Check(
                name="fee_variance_provided",
                passed=False,
                detail=f"claim.fee_variance missing or unparseable ({record.llm_claim.get('fee_variance')!r})",
            ))
            return _finalize(label, checks)
        fee_variance = fv
        checks.append(Check("fee_variance_provided", True, f"claim.fee_variance = Rs.{fee_variance}"))

    lhs = sum_settled + fee_variance
    gap = abs(lhs - order.net_expected)
    checks.append(Check(
        name="leg_sum_closes_to_net",
        passed=gap <= tol,
        detail=(
            f"sum(legs) Rs.{sum_settled}"
            + (f" + fee_variance Rs.{fee_variance}" if allow_fee_variance else "")
            + f" = Rs.{lhs}; net_expected Rs.{order.net_expected}; "
            + f"gap Rs.{gap}; tolerance Rs.{tol}"
        ),
    ))
    return _finalize(label, checks)


def _check_timing(record, order, in_pays, out_pays, recon_date, window) -> GuardVerdict:
    checks: list[Check] = []
    if order is None:
        checks.append(Check("order_exists", False, "no ledger order present"))
        return _finalize("TIMING_WINDOW", checks)
    checks.append(Check("order_exists", True, f"order {order.order_id}"))

    checks.append(Check(
        name="no_in_window_payouts",
        passed=len(in_pays) == 0,
        detail=(
            f"{len(in_pays)} in-window payout(s); TIMING_WINDOW requires zero "
            f"— any in-window payout means the discrepancy is about amounts/splits, not timing"
        ),
    ))
    if in_pays:
        return _finalize("TIMING_WINDOW", checks)

    checks.append(Check(
        name="has_out_of_window_payout",
        passed=len(out_pays) >= 1,
        detail=f"{len(out_pays)} out-of-window payout(s)",
    ))
    if not out_pays:
        return _finalize("TIMING_WINDOW", checks)

    late_ok = all(p.settled_at > recon_date for p in out_pays)
    checks.append(Check(
        name="out_of_window_payouts_after_cutoff",
        passed=late_ok,
        detail=(
            f"recon_cutoff = {recon_date.isoformat()}; "
            f"late payouts settled_at = {[p.settled_at.isoformat() for p in out_pays]}"
        ),
    ))
    return _finalize("TIMING_WINDOW", checks)


# --------------------------------------------------------------------------
# Batch orchestrator
# --------------------------------------------------------------------------

def verify_batch(
    records: list[ClassifiedRecord],
    decisions: list[MatchDecision],
    orders: list[Order],
    payouts: list[Payout],
    *,
    recon_date: datetime = DEFAULT_RECON_DATE,
    window: timedelta = DEFAULT_WINDOW,
) -> list[tuple[str, GuardVerdict]]:
    """Return one (key, verdict) per classified record, in input order."""
    from src.classifier.classify import build_contexts
    contexts_by_key = {c.decision.key: c for c in build_contexts(decisions, orders, payouts)}
    verdicts: list[tuple[str, GuardVerdict]] = []
    for r in records:
        ctx = contexts_by_key.get(r.key)
        if ctx is None:
            verdicts.append((r.key, GuardVerdict(
                accepted=False,
                final_category="GENUINE_MISMATCH",
                reason=f"No matcher context for key '{r.key}' — cannot verify.",
                checks=[Check("context_available", False, "no MatchDecision for this key")],
            )))
            continue
        verdicts.append((r.key, verify(
            r, ctx.order, ctx.in_window_payouts, ctx.out_of_window_payouts,
            recon_date=recon_date, window=window,
        )))
    return verdicts
