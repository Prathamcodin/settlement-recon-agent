"""
Capture the LLM classifier's output WITHOUT the validation guard.

This is an EVALUATION / EVIDENCE script — not part of the reconciler
itself. It legitimately reads ground_truth.json to score the LLM's
classifications and to identify any FALSE RECONCILIATIONS (cases where
the LLM claims a record reconciles under some accounting category, but
the ground truth says it is actually a GENUINE_MISMATCH).

The reconciler modules (src/matcher, src/classifier) still do not read
ground truth. That separation is enforced by the matcher's import test.
The scripts/ directory exists specifically to hold this kind of
evaluation tooling.

Output: evidence/before_guard.json — the "before" half of the
before/after evidence pair. The pitch says "N false reconciliations
before the guard, 0 after"; this script produces the N.

Usage:
  # Default: replay recorded responses from evidence/llm_raw/
  python scripts/capture_before_guard.py

  # One-time (or after prompt/batch changes): record live from the API
  python scripts/capture_before_guard.py --mode record
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make `src.*` imports work when this file is run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.classifier.classify import (
    DEFAULT_EVIDENCE_DIR,
    _build_backend,
    classify_escalations,
)
from src.matcher.match import load_orders, load_payouts, match_batch


def _load_ground_truth(path: Path) -> dict[str, dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {entry["key"]: entry for entry in payload["labels"]}


def _score(records, ground_truth):
    """
    Compare each LLM classification to ground truth.

    Buckets:
      correct               — LLM category matches ground truth
      false_reconciliation  — ground truth = GENUINE_MISMATCH but LLM
                              proposed a non-GENUINE category with
                              reconciles=true. The dangerous case.
      wrong_category        — LLM's category disagrees with ground truth
                              but the LLM did not claim reconciliation
                              (or ground truth is not GENUINE_MISMATCH).
                              A quality issue, not a safety issue.
    """
    correct: list[dict] = []
    false_recon: list[dict] = []
    wrong_cat: list[dict] = []
    for r in records:
        gt = ground_truth.get(r.key)
        if gt is None:
            # No ground-truth row for this key — shouldn't happen for the
            # seed=42 batch, but if it does, note and continue.
            wrong_cat.append({
                "key": r.key, "llm_category": r.llm_category,
                "ground_truth_category": "<missing>",
                "llm_reconciles": r.llm_reconciles,
                "llm_reasoning": r.llm_reasoning,
                "llm_claim": r.llm_claim,
            })
            continue

        entry = {
            "key": r.key,
            "matcher_strategy": r.matcher_strategy,
            "ground_truth_category": gt["expected_category"],
            "ground_truth_scenario": gt["scenario"],
            "llm_category": r.llm_category,
            "llm_reconciles": r.llm_reconciles,
            "llm_reasoning": r.llm_reasoning,
            "llm_claim": r.llm_claim,
        }
        gt_cat = gt["expected_category"]

        if r.llm_category == gt_cat:
            correct.append(entry)
        elif gt_cat == "GENUINE_MISMATCH" and r.llm_reconciles and r.llm_category != "GENUINE_MISMATCH":
            false_recon.append(entry)
        else:
            wrong_cat.append(entry)
    return correct, false_recon, wrong_cat


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture evidence/before_guard.json.")
    parser.add_argument("--orders", type=Path, default=Path("data/orders_ledger.csv"))
    parser.add_argument("--payouts", type=Path, default=Path("data/settlement_file.csv"))
    parser.add_argument("--ground-truth", type=Path, default=Path("data/ground_truth.json"))
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument("--out", type=Path, default=Path("evidence/before_guard.json"))
    parser.add_argument("--mode", choices=["replay", "record"], default="replay")
    args = parser.parse_args()

    orders = load_orders(args.orders)
    payouts = load_payouts(args.payouts)
    decisions = match_batch(orders, payouts)
    ground_truth = _load_ground_truth(args.ground_truth)

    backend = _build_backend() if args.mode == "record" else None
    if args.mode == "record":
        print(f"[record] backend={backend.name}, writing verbatim to {args.evidence_dir}/")

    records = classify_escalations(
        decisions, orders, payouts,
        mode=args.mode, backend=backend, evidence_dir=args.evidence_dir,
    )
    correct, false_recon, wrong_cat = _score(records, ground_truth)

    artifact = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "before_guard",
        "guard_applied": False,
        "batch_summary": {
            "total_records": len(decisions),
            "matched_deterministic": sum(1 for d in decisions if d.status == "MATCHED"),
            "escalated_to_llm": len(records),
        },
        "llm_summary": {
            "reconciled_by_llm": sum(1 for r in records if r.llm_reconciles),
            "confirmed_exception_by_llm": sum(1 for r in records if not r.llm_reconciles),
        },
        "accuracy_vs_ground_truth": {
            "correct": len(correct),
            "false_reconciliations": len(false_recon),
            "wrong_category": len(wrong_cat),
        },
        "false_reconciliations": false_recon,
        "wrong_category": wrong_cat,
        "correct": correct,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print(f"Wrote {args.out}")
    print(f"  total records            : {artifact['batch_summary']['total_records']}")
    print(f"  matched (deterministic)  : {artifact['batch_summary']['matched_deterministic']}")
    print(f"  escalated to LLM         : {artifact['batch_summary']['escalated_to_llm']}")
    print(f"  LLM says reconciles      : {artifact['llm_summary']['reconciled_by_llm']}")
    print(f"  LLM says exception       : {artifact['llm_summary']['confirmed_exception_by_llm']}")
    print()
    print(f"  Accuracy vs ground truth:")
    print(f"    correct                : {len(correct)}")
    print(f"    FALSE RECONCILIATIONS  : {len(false_recon)}   <-- this is the number the guard must drive to 0")
    print(f"    wrong category         : {len(wrong_cat)}")
    if false_recon:
        print()
        print("  False-reconciliation rows:")
        for r in false_recon:
            print(f"    {r['key']}: LLM said {r['llm_category']} (reconciles=True) -- "
                  f"ground truth is {r['ground_truth_category']} ({r['ground_truth_scenario']})")


if __name__ == "__main__":
    main()
