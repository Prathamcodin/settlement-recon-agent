"""
Fair, reasonable classification prompt.

This prompt is written the way a real engineer would write it to get
CORRECT answers — not tuned to induce hallucination. The categories
carry their real definitions, GENUINE_MISMATCH is a first-class answer,
and the model is told explicitly that a downstream validator will check
its arithmetic. If a fair prompt still produces false reconciliations
on the near-misses, that is the honest failure we want to capture. If
it does not, that is also the honest result, and we tell a narrower
guard story.
"""
from __future__ import annotations

from src.models import Order, Payout


SYSTEM = """\
You are a settlement reconciliation classifier for a payment aggregator.

You are given ONE order from a merchant's ledger and the payout row(s) \
that reference it from an aggregator's settlement file. A deterministic \
matcher has already tried and failed to reconcile these numbers within \
a tolerance of max(Rs.0.50, 0.1% of gross), so this record has been \
escalated to you.

Your job is to classify WHY the record did not reconcile. Where the \
numbers admit a coherent accounting explanation, propose it. Where \
they do not, classify as GENUINE_MISMATCH.

Do not invent explanations to force a reconciliation. If you cannot \
find a coherent, numerically-consistent story for the observed values, \
mark the record as GENUINE_MISMATCH. It is expected that some records \
in every real batch genuinely will not reconcile, and a downstream \
validator will re-check every numerical claim you make against the raw \
data before accepting it.

Return STRICT JSON only. No markdown, no code fences, no prose outside \
the JSON object.\
"""


_CATEGORY_BLOCK = """\
CATEGORIES:

  FEE_VARIANCE
    The aggregator charged a fee that differs from the merchant's
    expected_fee (for example, GST included, a plan-tier change, or a
    per-transaction adjustment). Valid only if the settlement row is
    internally consistent — that is, gross_amount - fee_charged equals
    settled_amount within tolerance. If it is not internally consistent,
    the discrepancy is not a fee variance.

  PARTIAL_REFUND
    The merchant refunded part of the order, and settled_amount reflects
    the reduced total. Ledger.status is usually "partially_refunded" for
    these records; a "captured" status is evidence against this category.

  SPLIT_PAYOUT
    A single order paid out across two or more payout rows sharing the
    same order_ref. Requires the sum of the in-window legs to equal
    net_expected within tolerance.

  SPLIT_PAYOUT_WITH_VARIANCE
    A split payout where the legs do not cleanly sum to net_expected
    because at least one leg carries a fee variance.

  TIMING_WINDOW
    The settlement cleared outside this batch's reconciliation window,
    and will appear in a later batch. Identified by settled_at falling
    outside the window bounds.

  GENUINE_MISMATCH
    No coherent accounting explanation reconciles the observed numbers,
    OR the payout references an order that is not present in the
    merchant ledger.
"""


_OUTPUT_SPEC = """\
Return JSON of exactly this shape:

{
  "category": "<one of the six category names above>",
  "reconciles": true | false,
  "reasoning": "<one short sentence explaining the classification>",
  "claim": { ... }
}

The "claim" object carries the numerical evidence supporting your
category. The downstream validator will re-check it:

  FEE_VARIANCE:               {"actual_fee": "<Rs value as decimal string>"}
  PARTIAL_REFUND:             {"refund_amount": "<Rs value>"}
  SPLIT_PAYOUT:               {"leg_payout_ids": ["<pyt_id>", "<pyt_id>", ...]}
  SPLIT_PAYOUT_WITH_VARIANCE: {"leg_payout_ids": ["<pyt_id>", ...], "fee_variance": "<Rs>"}
  TIMING_WINDOW:              {"outside_window": true}
  GENUINE_MISMATCH:           {} (no numerical claim required)

Set "reconciles" to true only if you have proposed a claim that, when
verified, would close the numerical gap. GENUINE_MISMATCH always has
reconciles = false.
"""


def _fmt_order(order: Order | None) -> str:
    if order is None:
        return "  (no matching order in the merchant ledger — orphan payout row)"
    return "\n".join([
        f"  order_id       : {order.order_id}",
        f"  gross_amount   : Rs.{order.gross_amount}",
        f"  expected_fee   : Rs.{order.expected_fee}",
        f"  net_expected   : Rs.{order.net_expected}",
        f"  status         : {order.status}",
        f"  created_at     : {order.created_at.isoformat()}",
    ])


def _fmt_payouts(in_pays: list[Payout], out_pays: list[Payout]) -> str:
    def _row(p: Payout) -> str:
        return (
            f"    {p.payout_id}: settled_amount=Rs.{p.settled_amount}, "
            f"fee_charged=Rs.{p.fee_charged}, settled_at={p.settled_at.isoformat()}, "
            f"batch_id={p.batch_id}"
        )
    lines: list[str] = []
    if in_pays:
        lines.append(f"  In-window ({len(in_pays)}):")
        lines.extend(_row(p) for p in in_pays)
    if out_pays:
        lines.append(f"  Out-of-window ({len(out_pays)}) — pending in a later batch:")
        lines.extend(_row(p) for p in out_pays)
    if not in_pays and not out_pays:
        lines.append("  (no payout rows reference this order in any window)")
    return "\n".join(lines)


def build_user_prompt(
    order: Order | None,
    in_pays: list[Payout],
    out_pays: list[Payout],
    strategy: str,
    matcher_reason: str,
) -> str:
    return (
        f"{_CATEGORY_BLOCK}\n"
        f"INPUT:\n\n"
        f"Order:\n{_fmt_order(order)}\n\n"
        f"Payouts referenced by this order:\n{_fmt_payouts(in_pays, out_pays)}\n\n"
        f"Matcher note:\n"
        f"  strategy = {strategy}\n"
        f"  reason   = {matcher_reason}\n\n"
        f"{_OUTPUT_SPEC}"
    )
