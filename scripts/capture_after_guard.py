"""
Run the validation guard over the hand-crafted fixtures and write
evidence/after_guard.json.

Why fixtures instead of the live batch: the live LLM run
(evidence/before_guard.json) produced 0 false reconciliations. There is
no dishonest claim in that batch for the guard to reject, so running
the guard against those recordings would produce a boring "everything
was already correct" artifact.

The point of after_guard.json is proving the guard's TEETH — that when
a false reconciliation DOES appear (as it will, at some point, on some
future batch or model version), the guard rejects it against hard
arithmetic and ledger evidence. The hand-crafted adversarial fixtures
in tests/fixtures/ are the worst-case LLM outputs we could not
observe live but must be prepared for.

Reads fixtures + ground_truth-agnostic evidence. Never reads
data/ground_truth.json — the fixtures encode their own expected
verdict, and the guard's job is to arrive at that verdict via
arithmetic, not from a labels file.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

# Make src.* imports work when this file is run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.classifier.classify import ClassifiedRecord
from src.guard.verify import verify
from src.models import Order, Payout


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _decode_order(data) -> Optional[Order]:
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


def _decode_payout(data) -> Payout:
    return Payout(
        payout_id=data["payout_id"],
        order_ref=data.get("order_ref"),
        settled_amount=Decimal(data["settled_amount"]),
        fee_charged=Decimal(data["fee_charged"]),
        settled_at=datetime.fromisoformat(data["settled_at"]),
        batch_id=data["batch_id"],
    )


def _load_fixture(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    ctx = data["context"]
    resp = data["llm_response"]
    return data, ClassifiedRecord(
        key=ctx["key"],
        matcher_strategy=ctx.get("matcher_strategy", "amount_mismatch"),
        llm_category=resp["category"],
        llm_reconciles=bool(resp.get("reconciles", False)),
        llm_reasoning=resp.get("reasoning", ""),
        llm_claim=resp.get("claim", {}) or {},
        llm_raw_response=json.dumps(resp),
        mode="fixture",
    ), _decode_order(ctx.get("order")), \
       [_decode_payout(p) for p in ctx.get("in_window_payouts", [])], \
       [_decode_payout(p) for p in ctx.get("out_of_window_payouts", [])]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the guard over all fixtures.")
    parser.add_argument("--fixtures-dir", type=Path, default=FIXTURES_DIR)
    parser.add_argument("--out", type=Path, default=Path("evidence/after_guard.json"))
    args = parser.parse_args()

    records = []
    counts = {
        "adversarial": {"total": 0, "rejected_by_guard": 0, "false_positives": 0},
        "positive":    {"total": 0, "accepted_by_guard": 0, "false_rejections": 0},
    }

    for fpath in sorted(args.fixtures_dir.glob("*.json")):
        meta, record, order, in_pays, out_pays = _load_fixture(fpath)
        verdict = verify(record, order, in_pays, out_pays)
        outcome_str = "REJECTED" if not verdict.accepted else "ACCEPTED"
        expected = meta["expected_guard"]
        matched_expectation = (
            (expected == "REJECT" and not verdict.accepted) or
            (expected == "ACCEPT" and verdict.accepted)
        )
        kind = meta["kind"]
        counts[kind]["total"] += 1
        if kind == "adversarial":
            if not verdict.accepted:
                counts[kind]["rejected_by_guard"] += 1
            else:
                counts[kind]["false_positives"] += 1
        else:
            if verdict.accepted:
                counts[kind]["accepted_by_guard"] += 1
            else:
                counts[kind]["false_rejections"] += 1

        records.append({
            "fixture": fpath.name,
            "kind": kind,
            "description": meta.get("description", ""),
            "llm_claim_category": record.llm_category,
            "llm_reconciles": record.llm_reconciles,
            "llm_claim": record.llm_claim,
            "expected_guard": expected,
            "guard_verdict": outcome_str,
            "guard_final_category": verdict.final_category,
            "guard_reason": verdict.reason,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in verdict.checks
            ],
            "matched_expectation": matched_expectation,
        })

    artifact = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "after_guard",
        "note": (
            "The guard was exercised against hand-crafted adversarial fixtures in "
            "tests/fixtures/ — LLM-shaped responses that WOULD be false "
            "reconciliations if produced live. The live batch (before_guard.json) "
            "produced 0 false reconciliations, so this artifact intentionally uses "
            "synthetic worst-case model outputs to prove the guard has teeth."
        ),
        "counts": counts,
        "records": records,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Wrote {args.out}")
    print(f"  adversarial : {counts['adversarial']['rejected_by_guard']}/"
          f"{counts['adversarial']['total']} rejected  "
          f"(false positives: {counts['adversarial']['false_positives']})")
    print(f"  positive    : {counts['positive']['accepted_by_guard']}/"
          f"{counts['positive']['total']} accepted   "
          f"(false rejections: {counts['positive']['false_rejections']})")
    all_expected = all(r["matched_expectation"] for r in records)
    print(f"  every fixture matched its expected verdict: {all_expected}")


if __name__ == "__main__":
    main()
