"""
Scale benchmark for the deterministic layers (and, optionally, MongoDB).

The reference batch is 63 records. This script generates the same
scenario mix multiplied by --scale (default 1600 -> 100,800 units),
runs the deterministic matcher on it, and scores every decision against
the generator's ground truth:

  false auto-match  matcher said MATCHED, but the record should have gone
                    to review. The dangerous error: money closed wrongly.
  missed match      matcher escalated a record it could have closed.
                    Safe but costly: more work for the LLM / reviewers.

What this does NOT do: call the LLM classifier. Escalations at this
scale have no recorded LLM responses (and 20,000 live calls would cost
money and could not be replayed), so here they take the pipeline's
fail-closed path: no classification means GENUINE_MISMATCH, i.e. a human
looks at it. The guard is tested separately against 170 generated lies
(scripts/capture_heldout_guard.py).

With MONGO_URL set, the script also saves the whole run to the audit
store and times the write, the reviewer's main query, and checks that the
query is served by the run_category index.

    python scripts/scale_benchmark.py                 # 100,800 units
    python scripts/scale_benchmark.py --scale 160     # ~10,000 units
    MONGO_URL=mongodb://localhost:27017 python scripts/scale_benchmark.py

Writes evidence/scale_benchmark.json.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.generator.generate import generate                       # noqa: E402
from src.matcher.match import load_orders, load_payouts, match_batch  # noqa: E402
from src.reporter.report import build_report                      # noqa: E402

# Scenarios the deterministic matcher is supposed to close on its own.
AUTO_MATCHABLE = {"clean", "rounding", "split_payout_clean"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--scale", type=int, default=1600)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path, default=Path("evidence/scale_benchmark.json"))
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        t = time.perf_counter()
        generate(seed=args.seed, out_dir=data_dir, scale=args.scale)
        gen_s = time.perf_counter() - t

        t = time.perf_counter()
        orders = load_orders(data_dir / "orders_ledger.csv")
        payouts = load_payouts(data_dir / "settlement_file.csv")
        load_s = time.perf_counter() - t

        t = time.perf_counter()
        decisions = match_batch(orders, payouts)
        match_s = time.perf_counter() - t

        labels = json.loads((data_dir / "ground_truth.json").read_text(encoding="utf-8"))["labels"]

    scenario_by_key = {lab["key"]: lab["scenario"] for lab in labels}
    false_auto_match = missed_match = 0
    for d in decisions:
        should_match = scenario_by_key[d.key] in AUTO_MATCHABLE
        if d.status == "MATCHED" and not should_match:
            false_auto_match += 1
        elif d.status != "MATCHED" and should_match:
            missed_match += 1
    matched = sum(1 for d in decisions if d.status == "MATCHED")

    result = {
        "scale": args.scale,
        "seed": args.seed,
        "machine": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
        "units": len(decisions),
        "orders": len(orders),
        "payouts": len(payouts),
        "matcher": {
            "matched": matched,
            "escalated": len(decisions) - matched,
            "match_rate": round(matched / len(decisions), 4),
            "false_auto_matches": false_auto_match,
            "missed_matches": missed_match,
            "seconds": round(match_s, 3),
            "units_per_second": round(len(decisions) / match_s),
        },
        "timings_seconds": {"generate": round(gen_s, 3), "load_csv": round(load_s, 3)},
        "scenario_mix": dict(Counter(lab["scenario"] for lab in labels)),
    }

    store_result = _benchmark_store(decisions)
    if store_result is not None:
        result["mongodb"] = store_result

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    _print(result)
    print(f"\nwrote {args.out}")


def _benchmark_store(decisions) -> dict | None:
    if not os.environ.get("MONGO_URL"):
        return None
    from src.store.mongo_store import store_from_env

    store = store_from_env()
    # No LLM at this scale: escalations take the fail-closed path.
    report = build_report(decisions, [], [])

    t = time.perf_counter()
    run_id = store.save_run(report, source="scale_benchmark")
    save_s = time.perf_counter() - t

    t = time.perf_counter()
    exceptions = store.find_decisions(run_id, category="GENUINE_MISMATCH", limit=100)
    query_ms = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    breakdown = store.category_counts(run_id)
    agg_ms = (time.perf_counter() - t) * 1000

    plan = store.decisions.find(
        {"run_id": run_id, "final_category": "GENUINE_MISMATCH"}
    ).explain()["queryPlanner"]["winningPlan"]
    index_used = _index_name(plan)

    return {
        "run_id": run_id,
        "documents_written": len(report["records"]),
        "save_seconds": round(save_s, 3),
        "documents_per_second": round(len(report["records"]) / save_s),
        "exception_query_ms_first_100": round(query_ms, 2),
        "exceptions_returned": len(exceptions),
        "category_aggregation_ms": round(agg_ms, 2),
        "category_counts": breakdown,
        "exception_query_index": index_used,
    }


def _index_name(plan: dict) -> str | None:
    """Walk a MongoDB query plan down to the index scan stage, if any."""
    while plan:
        if plan.get("stage") == "IXSCAN":
            return plan.get("indexName")
        plan = plan.get("inputStage") or (plan.get("queryPlan") or {})
    return None


def _print(r: dict) -> None:
    m = r["matcher"]
    print(f"units              {r['units']:,}  ({r['orders']:,} orders, {r['payouts']:,} payouts)")
    print(f"matcher            {m['units_per_second']:,} units/s  ({m['seconds']} s)")
    print(f"auto-matched       {m['matched']:,}  ({m['match_rate'] * 100:.1f}%)")
    print(f"false auto-matches {m['false_auto_matches']}")
    print(f"missed matches     {m['missed_matches']}")
    if "mongodb" in r:
        s = r["mongodb"]
        print(f"mongodb write      {s['documents_written']:,} docs in {s['save_seconds']} s "
              f"({s['documents_per_second']:,} docs/s)")
        print(f"exception query    {s['exception_query_ms_first_100']} ms "
              f"(index: {s['exception_query_index']})")
        print(f"category breakdown {s['category_aggregation_ms']} ms")


if __name__ == "__main__":
    main()
