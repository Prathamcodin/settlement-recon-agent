"""
Shared domain types.

Everything that flows through the pipeline (generator → matcher → LLM
classifier → guard → reporter) speaks in these types. Centralizing them
avoids each layer re-declaring its own near-identical dataclass and
drifting.

Money is `Decimal` everywhere. We never let float arithmetic decide
whether a settlement reconciles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional


@dataclass
class Order:
    """A row from the merchant's internal orders ledger."""
    order_id: str
    gross_amount: Decimal
    expected_fee: Decimal
    net_expected: Decimal
    status: str                    # "captured" | "partially_refunded" | "refunded"
    created_at: datetime


@dataclass
class Payout:
    """A row from the aggregator's settlement file."""
    payout_id: str
    order_ref: Optional[str]       # None when the row has no order reference
    settled_amount: Decimal
    fee_charged: Decimal
    settled_at: datetime
    batch_id: str


@dataclass
class MatchDecision:
    """
    The deterministic matcher's verdict for one reconciliation unit.

    A unit is usually one order, but can also be an orphan payout (a
    settlement row with no corresponding ledger entry) — in which case
    `key` is the phantom order_ref.

    `matched_payout_ids` records every in-window payout we associated
    with this key, including cases where the association failed —
    critical for a traceable audit trail on splits.
    `out_of_window_payout_ids` surfaces late legs so the LLM (and the
    reader) know some data is still pending.
    `tolerance_used` and `tolerance_gap` together produce the audit
    phrase "absorbed Rs.X against a Rs.Y ceiling."
    """
    key: str
    status: str                                # "MATCHED" | "UNMATCHED_ESCALATE"
    strategy: str                              # see matcher.match for the enumeration
    matched_payout_ids: list[str] = field(default_factory=list)
    out_of_window_payout_ids: list[str] = field(default_factory=list)
    tolerance_used: Optional[Decimal] = None
    tolerance_gap: Optional[Decimal] = None
    reason: str = ""
