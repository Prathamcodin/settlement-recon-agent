"""
Stress the validation guard with tens of thousands of generated cases.

The held-out suite (scripts/capture_heldout_guard.py) runs 250 cases on
one seed. This runs the same suite multiplied by --scale on several
seeds, through the actual guard (src/guard/verify.py, unchanged), and
records every failure. No LLM is involved: the lies are synthesized
mechanically, the way the held-out suite builds them.

    python scripts/stress_guard.py                       # 5 seeds x scale 40 = 50,000 cases
    python scripts/stress_guard.py --scale 4 --seeds 7   # quick run

Writes evidence/guard_stress.json (a summary plus every failing case,
not the 50,000 passing ones).

Scope: more of the modeled lie types (smart fee lie, wrong-status refund
lie, ghost split, orphan fee/refund claims) and legitimate cases, across
many randomized amounts, fee rates and seeds. It shows the guard's checks
hold across the input space; it does not invent new kinds of lies.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.heldout import run_heldout_suite   # noqa: E402

DEFAULT_SEEDS = [1337, 2024, 7, 99, 31337]


def main() -> None:
    parser = argparse.ArgumentParser(description="Stress-test the validation guard.")
    parser.add_argument("--scale", type=int, default=40,
                        help="Multiply the 250-case held-out suite (default 40 -> 10,000 per seed)")
    parser.add_argument("--seeds", type=int, nargs="+", default=DEFAULT_SEEDS)
    parser.add_argument("--out", type=Path, default=Path("evidence/guard_stress.json"))
    args = parser.parse_args()

    per_seed = []
    by_lie_kind: Counter = Counter()
    failures: list[dict] = []
    t0 = time.perf_counter()
    for seed in args.seeds:
        r = run_heldout_suite(seed=seed, scale=args.scale)
        per_seed.append({
            "seed": seed,
            "cases": r["cases_total"],
            "adversarial_rejected": r["adversarial"]["rejected_by_guard"],
            "adversarial_total": r["adversarial"]["total"],
            "legit_accepted": r["legit"]["accepted_by_guard"],
            "legit_total": r["legit"]["total"],
        })
        for o in r["outcomes"]:
            by_lie_kind[o["lie_kind"]] += 1
            if not o["pass"]:
                failures.append({"seed": seed, **o})
    seconds = time.perf_counter() - t0

    total = sum(s["cases"] for s in per_seed)
    adv_total = sum(s["adversarial_total"] for s in per_seed)
    adv_rejected = sum(s["adversarial_rejected"] for s in per_seed)
    legit_total = sum(s["legit_total"] for s in per_seed)
    legit_accepted = sum(s["legit_accepted"] for s in per_seed)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scale": args.scale,
        "seeds": args.seeds,
        "cases_total": total,
        "adversarial": {"total": adv_total, "rejected_by_guard": adv_rejected,
                        "accepted_by_mistake": adv_total - adv_rejected},
        "legit": {"total": legit_total, "accepted_by_guard": legit_accepted,
                  "rejected_by_mistake": legit_total - legit_accepted},
        "cases_by_type": dict(sorted(by_lie_kind.items())),
        "seconds": round(seconds, 2),
        "per_seed": per_seed,
        "failures": failures,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"cases              {total:,} across {len(args.seeds)} seeds ({seconds:.2f} s)")
    print(f"lies rejected      {adv_rejected:,}/{adv_total:,}")
    print(f"legit accepted     {legit_accepted:,}/{legit_total:,}")
    print("cases by type      " + ", ".join(f"{k} {v:,}" for k, v in sorted(by_lie_kind.items())))
    print(f"\nwrote {args.out}")
    if failures:
        print(f"\n{len(failures)} FAILURES, first 5:")
        for f in failures[:5]:
            print(f"  seed {f['seed']} {f['case_id']} {f['lie_kind']}: {f['guard_verdict']} "
                  f"claim={f['llm_claim']} reason={f['guard_reason'][:150]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
