"""
End-to-end pipeline runner.

  data (regen if missing) --> matcher --> classifier (replay) --> guard
                                                                   |
                                                                   v
                                                                 report
                                                                   |
                                                                   v
                                                              summary print

Replay-by-default: no key, no network calls are needed at runtime. Data
regenerates from the seeded generator; LLM responses replay from
committed evidence/llm_raw/ recordings.

Pass --record-live to swap the classifier into record mode, which
requires google-genai + GEMINI_API_KEY + network. That path writes
new recordings to evidence/llm_raw/ and is the way the committed
recordings were produced in the first place.

Terminal output uses `rich` when available for a polished demo; if
rich is not installed, falls back to the plain-text `format_summary_text`
so the pipeline still runs and is scriptable. Rich is presentation only:
no numbers, layers, or logic depend on whether it is loaded.
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


try:
    from rich.console import Console
    from src.reporter.render_rich import render_final_report, stage
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False


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
    parser.add_argument("--plain", action="store_true",
                        help="Force plain-text output even if rich is available.")
    args = parser.parse_args()

    use_rich = _HAS_RICH and not args.plain

    orders_path = args.data_dir / "orders_ledger.csv"
    payouts_path = args.data_dir / "settlement_file.csv"

    if use_rich:
        console = Console()
        _run_rich(args, console, orders_path, payouts_path)
    else:
        _run_plain(args, orders_path, payouts_path)


def _run_rich(args, console, orders_path, payouts_path) -> None:
    # DATA
    stage(console, "DATA")
    if args.regen or not orders_path.exists() or not payouts_path.exists():
        console.print(f"[dim]regenerating batch (seed={args.seed}) -> {args.data_dir}/[/]")
        generate(seed=args.seed, out_dir=args.data_dir)
    orders = load_orders(orders_path)
    payouts = load_payouts(payouts_path)
    console.print(f"[green]OK[/]  loaded [bold]{len(orders)}[/] orders  ·  "
                  f"[bold]{len(payouts)}[/] payouts")

    # MATCHER
    stage(console, "DETERMINISTIC MATCHER", "src/matcher · pure logic, no LLM")
    decisions = match_batch(orders, payouts)
    matched = sum(1 for d in decisions if d.status == "MATCHED")
    escalated = len(decisions) - matched
    console.print(f"[green]{matched}[/]/[bold]{len(decisions)}[/] matched  "
                  f"([green]{matched / len(decisions) * 100:.1f}%[/])")
    console.print(f"[yellow]{escalated}[/] escalated to LLM")

    # CLASSIFIER
    stage(console, "LLM CLASSIFIER",
          "Gemini 3.6 Flash · replay from evidence/llm_raw/")
    backend = None
    mode = "replay"
    if args.record_live:
        mode = "record"
        from src.classifier.classify import _build_backend
        backend = _build_backend()
        console.print(f"[bold yellow]record-live[/] backend={backend.name} "
                      f"-> writing verbatim to {args.evidence_dir}/")
    records = classify_escalations(
        decisions, orders, payouts,
        mode=mode, backend=backend, evidence_dir=args.evidence_dir,
    )
    reconciles = sum(1 for r in records if r.llm_reconciles)
    console.print(f"[green]OK[/]  [bold]{len(records)}[/] classifications  "
                  f"([cyan]{reconciles}[/] reconciles · "
                  f"[dim]{len(records) - reconciles} exception[/])")

    # GUARD
    stage(console, "VALIDATION GUARD",
          "src/guard · every claim re-checked against raw settlement data")
    verdicts = verify_batch(records, decisions, orders, payouts)
    accepted = sum(1 for _, v in verdicts if v.accepted)
    rejected = len(verdicts) - accepted
    console.print(f"[green]{accepted}[/] accepted")
    if rejected > 0:
        console.print(f"[bold red]{rejected} REJECTED[/] -> forced to GENUINE_MISMATCH")
    else:
        console.print(f"[dim]{rejected} rejected on this live batch[/]")

    # REPORT
    report = build_report(decisions, records, verdicts)
    live_source = _read_json_if_present(Path("evidence/before_guard.json"))
    adv_source = _read_json_if_present(Path("evidence/after_guard.json"))
    render_final_report(console, report, live_source=live_source, adv_source=adv_source)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        console.print(f"\n[dim]wrote full JSON report to[/] {args.out}")


def _run_plain(args, orders_path, payouts_path) -> None:
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

    live_source = _read_json_if_present(Path("evidence/before_guard.json"))
    adv_source = _read_json_if_present(Path("evidence/after_guard.json"))
    print(format_summary_text(report,
                              live_batch_source=live_source,
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
