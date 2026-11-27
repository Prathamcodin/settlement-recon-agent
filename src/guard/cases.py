"""
Guard cases — the hand-crafted guard fixtures, run live.

tests/fixtures/ holds LLM-shaped responses written to attack the guard
(six adversarial fabrications, including the arithmetically-perfect
"smart lie") plus four honest positive controls. The test suite asserts
the verdicts; this module runs the same fixtures through the real guard
on request, so the API (and the dashboard's Guard Lab page) can show
each lie being caught with the exact check that caught it.

The fixtures stay hand-crafted and separate from evidence/llm_raw/
(real model output): nothing here is presented as something a model said.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from src.classifier.classify import ClassifiedRecord
from src.guard.verify import verify
from src.models import Order, Payout

DEFAULT_CASES_DIR = Path(os.environ.get("GUARD_CASES_DIR", "tests/fixtures"))


def _order(data: Optional[dict]) -> Optional[Order]:
    if data is None:
        return None
    return Order(
        order_id=data["order_id"],
        gross_amount=Decimal(data["gross_amount"]),
        expected_fee=Decimal(data["expected_fee"]),
        net_expected=Decimal(data["net_expected"]),
        status=data["status"],
        created_at=datetime.fromisoformat(data["created_at"]),
    )


def _payout(data: dict) -> Payout:
    return Payout(
        payout_id=data["payout_id"],
        order_ref=data.get("order_ref"),
        settled_amount=Decimal(data["settled_amount"]),
        fee_charged=Decimal(data["fee_charged"]),
        settled_at=datetime.fromisoformat(data["settled_at"]),
        batch_id=data["batch_id"],
    )


def run_case(data: dict) -> dict:
    """Run one fixture through the guard and return it with the verdict."""
    ctx = data["context"]
    resp = data["llm_response"]
    record = ClassifiedRecord(
        key=ctx["key"],
        matcher_strategy=ctx.get("matcher_strategy", "amount_mismatch"),
        llm_category=resp["category"],
        llm_reconciles=bool(resp.get("reconciles", False)),
        llm_reasoning=resp.get("reasoning", ""),
        llm_claim=resp.get("claim", {}) or {},
        llm_raw_response=json.dumps(resp),
        mode="fixture",
    )
    verdict = verify(
        record,
        _order(ctx.get("order")),
        [_payout(p) for p in ctx.get("in_window_payouts", [])],
        [_payout(p) for p in ctx.get("out_of_window_payouts", [])],
    )
    actual = "ACCEPT" if verdict.accepted else "REJECT"
    return {
        "name": data["name"],
        "kind": data["kind"],
        "description": data["description"],
        "context": ctx,
        "llm_response": resp,
        "expected_guard": data["expected_guard"],
        "guard_verdict": actual,
        "guard_correct": actual == data["expected_guard"],
        "final_category": verdict.final_category,
        "guard_reason": verdict.reason,
        "guard_checks": [{"name": c.name, "passed": c.passed, "detail": c.detail}
                         for c in verdict.checks],
    }


def run_all_cases(cases_dir: Path = DEFAULT_CASES_DIR) -> list[dict]:
    """Every fixture, adversarial first, each with the guard's live verdict."""
    results = [run_case(json.loads(f.read_text(encoding="utf-8")))
               for f in sorted(Path(cases_dir).glob("*.json"))]
    return sorted(results, key=lambda r: (r["kind"] != "adversarial", r["name"]))
