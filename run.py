"""
End-to-end pipeline runner.

  data (regen if missing) --> matcher --> classifier (replay) --> guard
                                                                   |
                                                                   v
                                                                 report
                                                                   |
                                                                   v
                                                              summary print

Replay-by-default: no key, no network, no third-party pip installs are
needed to run this file on a fresh clone. Everything the pipeline
touches is either Python stdlib or committed to the repo (data is
regenerated on-demand from the seeded generator; LLM responses are
replayed from the committed evidence/llm_raw/ recordings).

Pass --record-live to swap the classifier into record mode, which
does require google-genai + GEMINI_API_KEY + network. That path
writes new recordings to evidence/llm_raw/ and is the way the
committed recordings were produced in the first place.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.classifier.classify import DEFAULT_EVIDENCE_DIR, classify_escalations
from src.generator.generate import generate
from src.guard.verify import verify_batch
from src.matcher.match import load_orders, load_payouts, match_batch
from src.reporter.report import build_report, format_summary_text


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the reconciliation pipeline end-to-end.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument("--seed", type=int, default=42,
                        help="Generator seed; must match seed used for committed recordings.")
    parser.add_argument("--regen", action="store_true",
                        help="Force regenerate data even if it already exists.")
    parser.add_argument("--record-live", action="store_true",
                        help="Swap classifier into record mode (requires GEMINI_API_KEY).")
    parser.add_argument("--out", type=Path, default=None,
                        help="Optional path to write the full JSON report.")
    args = parser.parse_args()

    orders_path = args.data_dir / "orders_ledger.csv"
    payouts_path = args.data_dir / "settlement_file.csv"

    if args.regen or not orders_path.exists() or not payouts_path.exists():
        print(f"[data] generating (seed={args.seed}) -> {args.data_dir}/")
        generate(seed=args.seed, out_dir=args.data_dir)

    orders = load_orders(orders_path)
    payouts = load_payouts(payouts_path)
    decisions = match_batch(orders, payouts)

    backend = None
    mode = "replay"
    if args.record_live:
        mode = "record"
        from src.classifier.classify import _build_backend
        backend = _build_backend()
        print(f"[record-live] classifier backend={backend.name}, writing to {args.evidence_dir}/")

    records = classify_escalations(
        decisions, orders, payouts,
        mode=mode, backend=backend, evidence_dir=args.evidence_dir,
    )
    verdicts = verify_batch(records, decisions, orders, payouts)
    report = build_report(decisions, records, verdicts)

    # The two-label safety finding is sourced from committed artifacts,
    # NOT re-computed here — the pipeline itself never reads ground truth.
    live_batch_source = _read_json_if_present(Path("evidence/before_guard.json"))
    adv_source = _read_json_if_present(Path("evidence/after_guard.json"))

    print(format_summary_text(report,
                              live_batch_source=live_batch_source,
                              adv_source=adv_source))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nWrote full JSON report to {args.out}")


def _read_json_if_present(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


if __name__ == "__main__":
    main()
