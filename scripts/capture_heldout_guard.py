"""
Run the held-out adversarial suite and write evidence/heldout_guard.json.

This is stress-test evidence separate from evidence/after_guard.json (which
uses the hand-crafted fixtures in tests/fixtures/). Here the records are
RANDOMIZED (seed=1337, distinct from the main batch's seed=42) and the lies
are MECHANICALLY SYNTHESIZED — none of the specific numbers were chosen by
hand.

Scope: this stress-tests the modeled dangerous-lie class (arithmetically-
self-consistent fabrications that contradict the settlement's recorded data,
plus ledger-status refutations, plus ghost-leg splits, plus orphan claims)
across randomized inputs. It does not claim to cover every possible novel
lie type. Anything the guard cannot verify fails closed (unknown categories
are always rejected — see src/guard/verify.py).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.heldout import run_heldout_suite


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the held-out adversarial suite.")
    parser.add_argument("--seed", type=int, default=1337,
                        help="RNG seed for record generation (distinct from the main seed=42).")
    parser.add_argument("--out", type=Path, default=Path("evidence/heldout_guard.json"))
    args = parser.parse_args()

    results = run_heldout_suite(seed=args.seed)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "heldout_guard",
        "note": (
            "Held-out stress test of the validation guard. Records were "
            "generated with a random seed distinct from the main batch and "
            "none of the numbers were hand-authored; lies were mechanically "
            "synthesized. Scope: exercises the modeled dangerous-lie class "
            "across randomized inputs. Does not claim to cover every possible "
            "novel lie type. Categories the guard cannot verify fail closed."
        ),
        **results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    a = payload["adversarial"]
    l = payload["legit"]
    print(f"Wrote {args.out}")
    print(f"  seed              : {payload['seed']}")
    print(f"  records generated : {payload['records_generated']}")
    print(f"  cases total       : {payload['cases_total']}")
    print(f"  adversarial       : {a['rejected_by_guard']}/{a['total']} rejected  "
          f"(false positives: {a['false_positives']})")
    print(f"  legit             : {l['accepted_by_guard']}/{l['total']} accepted   "
          f"(false rejections: {l['false_rejections']})")


if __name__ == "__main__":
    main()
