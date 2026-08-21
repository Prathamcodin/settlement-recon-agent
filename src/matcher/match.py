"""
Deterministic matcher — layer 1 of the reconciliation pipeline.

The trusted core. Given a batch of orders and payouts, it produces a
MatchDecision for every reconciliation unit (each order plus each orphan
payout). No LLM anywhere in this file. No I/O beyond reading two CSVs
and printing a summary.

Hard invariant: this module must never import from
`src.generator` and must never read `ground_truth.json`. The matcher
measures the world; the ground truth is only for tests and the final
accuracy report.

Strategies (audit-log values):

    MATCHED:
      exact              — 1 payout, gap == 0
      within_tolerance   — 1 payout, gap <= tolerance
      split_sum          — 2+ payouts, sum matches (exact or within tol)

    UNMATCHED_ESCALATE:
      no_payouts_found             — order has no payout anywhere
      all_payouts_out_of_window    — order has payouts, none in-window
      amount_mismatch              — 1 payout in-window, gap > tolerance
      split_incomplete             — 2+ payouts, sum off (see reason tag)
      orphan_payout                — payout references unknown order_ref

For split_incomplete, the reason string carries a tag:
      [late_leg]     — at least one leg is out-of-window (data missing)
      [sum_mismatch] — all legs in-window, but sums still don't reconcile

late_leg beats sum_mismatch because "missing data" is a stronger reason
to defer than "inconsistent data" — the missing leg might close the gap.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from src.models import MatchDecision, Order, Payout
from src.tolerance import rounding_tolerance


# Batch defaults. In production these would come from batch config; here
# we hardcode to keep the end-to-end demo runnable in one command.
DEFAULT_RECON_DATE = datetime(2026, 8, 20, 18, 0, 0, tzinfo=timezone.utc)
DEFAULT_WINDOW = timedelta(hours=24)


# --------------------------------------------------------------------------
# CSV loaders — Decimal all the way, no float drift
# --------------------------------------------------------------------------

def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def load_orders(path: Path) -> list[Order]:
    with Path(path).open(encoding="utf-8", newline="") as f:
        return [
            Order(
                order_id=row["order_id"],
                gross_amount=Decimal(row["gross_amount"]),
                expected_fee=Decimal(row["expected_fee"]),
                net_expected=Decimal(row["net_expected"]),
                status=row["status"],
                created_at=_parse_iso(row["created_at"]),
            )
            for row in csv.DictReader(f)
        ]


def load_payouts(path: Path) -> list[Payout]:
    with Path(path).open(encoding="utf-8", newline="") as f:
        return [
            Payout(
                payout_id=row["payout_id"],
                order_ref=row["order_ref"] or None,
                settled_amount=Decimal(row["settled_amount"]),
                fee_charged=Decimal(row["fee_charged"]),
                settled_at=_parse_iso(row["settled_at"]),
                batch_id=row["batch_id"],
            )
            for row in csv.DictReader(f)
        ]


# --------------------------------------------------------------------------
# Core matcher — pure function, no I/O
# --------------------------------------------------------------------------

def match_batch(
    orders: Iterable[Order],
    payouts: Iterable[Payout],
    recon_date: datetime = DEFAULT_RECON_DATE,
    window: timedelta = DEFAULT_WINDOW,
) -> list[MatchDecision]:
    orders = list(orders)
    payouts = list(payouts)
    orders_by_id = {o.order_id: o for o in orders}

    window_start = recon_date - window
    window_end = recon_date

    in_window: dict[str | None, list[Payout]] = defaultdict(list)
    out_of_window: dict[str | None, list[Payout]] = defaultdict(list)
    for p in payouts:
        bucket = in_window if window_start <= p.settled_at <= window_end else out_of_window
        bucket[p.order_ref].append(p)

    decisions: list[MatchDecision] = []

    # One decision per order (order-side view).
    for order in orders:
        decisions.append(_decide_for_order(
            order,
            in_pays=in_window.get(order.order_id, []),
            out_pays=out_of_window.get(order.order_id, []),
        ))

    # Orphan payouts (payout-side view): in-window payout rows whose
    # order_ref has no matching ledger entry. We emit one decision per
    # orphan payout to preserve audit fidelity when several orphans
    # somehow share the same fake ref.
    for order_ref, pays in in_window.items():
        if order_ref in orders_by_id:
            continue
        for p in pays:
            decisions.append(MatchDecision(
                key=order_ref or f"<no_order_ref:{p.payout_id}>",
                status="UNMATCHED_ESCALATE",
                strategy="orphan_payout",
                matched_payout_ids=[p.payout_id],
                out_of_window_payout_ids=[],
                tolerance_used=None,
                tolerance_gap=None,
                reason=(
                    f"Payout {p.payout_id} references order_ref "
                    f"'{order_ref}' with no matching ledger entry."
                ),
            ))

    return decisions


def _decide_for_order(
    order: Order,
    in_pays: list[Payout],
    out_pays: list[Payout],
) -> MatchDecision:
    key = order.order_id
    tol = rounding_tolerance(order.gross_amount)
    out_ids = [p.payout_id for p in out_pays]

    # No payouts anywhere.
    if not in_pays and not out_pays:
        return MatchDecision(
            key=key,
            status="UNMATCHED_ESCALATE",
            strategy="no_payouts_found",
            matched_payout_ids=[],
            out_of_window_payout_ids=[],
            tolerance_used=None,
            tolerance_gap=None,
            reason="No payout row references this order in any window.",
        )

    # Order has payouts, but every one landed outside the window.
    if not in_pays:
        return MatchDecision(
            key=key,
            status="UNMATCHED_ESCALATE",
            strategy="all_payouts_out_of_window",
            matched_payout_ids=[],
            out_of_window_payout_ids=out_ids,
            tolerance_used=None,
            tolerance_gap=None,
            reason=(
                f"All {len(out_pays)} payout(s) for this order settled "
                f"outside the reconciliation window."
            ),
        )

    # At least one in-window payout. Try to close the amount.
    in_ids = [p.payout_id for p in in_pays]
    sum_settled = sum((p.settled_amount for p in in_pays), Decimal("0"))
    gap = abs(order.net_expected - sum_settled)
    # Split candidacy is based on TOTAL payouts, not just in-window ones.
    # A single in-window leg with a sibling out-of-window leg is still a
    # split — one that must escalate as late_leg, not amount_mismatch.
    is_split_candidate = (len(in_pays) + len(out_pays)) >= 2
    within_tol = gap <= tol

    # A MATCH requires within-tolerance closure AND (for splits) no late legs.
    # Rationale: even if the in-window sum happens to reconcile, the presence
    # of an out-of-window sibling means more money is coming or something is
    # duplicated — either way, escalate.
    if within_tol and not out_pays:
        if is_split_candidate:
            strategy = "split_sum"
            reason = (
                f"Sum of {len(in_pays)} legs = Rs.{sum_settled} vs "
                f"net_expected Rs.{order.net_expected}; absorbed gap "
                f"Rs.{gap} within tolerance Rs.{tol}."
            )
        elif gap == 0:
            strategy = "exact"
            reason = f"Single payout Rs.{sum_settled} matches net_expected exactly."
        else:
            strategy = "within_tolerance"
            reason = (
                f"Single payout Rs.{sum_settled} vs net_expected "
                f"Rs.{order.net_expected}; absorbed gap Rs.{gap} against "
                f"a Rs.{tol} ceiling."
            )
        return MatchDecision(
            key=key,
            status="MATCHED",
            strategy=strategy,
            matched_payout_ids=in_ids,
            out_of_window_payout_ids=out_ids,
            tolerance_used=tol,
            tolerance_gap=gap,
            reason=reason,
        )

    # Escalation — split precedence rule.
    if is_split_candidate:
        if out_pays:
            reason_tag = "late_leg"
            body = (
                f"{len(in_pays)} in-window leg(s) sum to Rs.{sum_settled} "
                f"vs net_expected Rs.{order.net_expected} (gap Rs.{gap} > "
                f"tolerance Rs.{tol}); {len(out_pays)} leg(s) pending in a "
                f"later batch."
            )
        else:
            reason_tag = "sum_mismatch"
            body = (
                f"{len(in_pays)} legs sum to Rs.{sum_settled} vs "
                f"net_expected Rs.{order.net_expected} (gap Rs.{gap} > "
                f"tolerance Rs.{tol}); no additional legs pending."
            )
        return MatchDecision(
            key=key,
            status="UNMATCHED_ESCALATE",
            strategy="split_incomplete",
            matched_payout_ids=in_ids,
            out_of_window_payout_ids=out_ids,
            tolerance_used=tol,
            tolerance_gap=gap,
            reason=f"[{reason_tag}] Split candidate: {body}",
        )

    return MatchDecision(
        key=key,
        status="UNMATCHED_ESCALATE",
        strategy="amount_mismatch",
        matched_payout_ids=in_ids,
        out_of_window_payout_ids=out_ids,
        tolerance_used=tol,
        tolerance_gap=gap,
        reason=(
            f"Single payout Rs.{sum_settled} vs net_expected "
            f"Rs.{order.net_expected}; gap Rs.{gap} > tolerance Rs.{tol}."
        ),
    )


# --------------------------------------------------------------------------
# Summary + CLI
# --------------------------------------------------------------------------

def summarize(decisions: list[MatchDecision]) -> str:
    total = len(decisions)
    matched = sum(1 for d in decisions if d.status == "MATCHED")
    escalated = total - matched
    by_strategy = Counter(d.strategy for d in decisions)
    lines = [
        "Deterministic matcher summary",
        f"  total decisions : {total}",
        f"  MATCHED         : {matched:>3}  ({matched / total * 100:5.1f}%)",
        f"  ESCALATED       : {escalated:>3}  ({escalated / total * 100:5.1f}%)",
        "  strategy breakdown:",
    ]
    for strategy, count in sorted(by_strategy.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"    {strategy:32s} {count}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the deterministic reconciliation matcher.")
    parser.add_argument("--orders", type=Path, default=Path("data/orders_ledger.csv"))
    parser.add_argument("--payouts", type=Path, default=Path("data/settlement_file.csv"))
    args = parser.parse_args()

    orders = load_orders(args.orders)
    payouts = load_payouts(args.payouts)
    decisions = match_batch(orders, payouts)
    print(summarize(decisions))


if __name__ == "__main__":
    main()
