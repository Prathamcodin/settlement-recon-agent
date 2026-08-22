"""
Reporter — layer 4 of the pipeline.

Takes the per-record outputs from the matcher, the LLM classifier, and
the guard, and merges them by key into:

  1. A structured `report` dict — the API returns this as JSON, and the
     CLI can write it to disk with `--out`.
  2. A human-readable summary — the CLI prints this at the end of every
     run. Its final section is the "two-label safety finding": the live
     LLM batch and the adversarial guard test as SEPARATE labeled facts,
     never collapsed into a single "0 -> 0" line that would make the
     guard look idle on a clean batch.

The reporter reads nothing from ground_truth.json — the pipeline itself
stays honest. The two-label safety finding is composed from committed
evidence artifacts (evidence/before_guard.json and after_guard.json),
which are the outputs of separate evaluation scripts.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from src.classifier.classify import ClassifiedRecord
from src.guard.verify import GuardVerdict
from src.models import MatchDecision


@dataclass
class RecordReport:
    key: str
    matcher_status: str
    matcher_strategy: str
    matcher_matched_payout_ids: list[str]
    matcher_reason: str
    llm_category: Optional[str] = None
    llm_reconciles: Optional[bool] = None
    llm_reasoning: Optional[str] = None
    llm_claim: Optional[dict] = None
    guard_verdict: Optional[str] = None            # "ACCEPTED" | "REJECTED"
    guard_reason: Optional[str] = None
    guard_checks: list[dict] = field(default_factory=list)
    final_category: str = ""


def build_report(
    decisions: list[MatchDecision],
    classified: list[ClassifiedRecord],
    verdicts: list[tuple[str, GuardVerdict]],
) -> dict:
    """Merge the three layers by key into a single structured report."""
    classified_by_key = {r.key: r for r in classified}
    verdict_by_key = dict(verdicts)
    records: list[RecordReport] = []

    for d in decisions:
        r = RecordReport(
            key=d.key,
            matcher_status=d.status,
            matcher_strategy=d.strategy,
            matcher_matched_payout_ids=list(d.matched_payout_ids),
            matcher_reason=d.reason,
        )
        if d.status == "MATCHED":
            r.final_category = "MATCHED"
        else:
            cr = classified_by_key.get(d.key)
            gv = verdict_by_key.get(d.key)
            if cr is not None:
                r.llm_category = cr.llm_category
                r.llm_reconciles = cr.llm_reconciles
                r.llm_reasoning = cr.llm_reasoning
                r.llm_claim = cr.llm_claim
            if gv is not None:
                r.guard_verdict = "ACCEPTED" if gv.accepted else "REJECTED"
                r.guard_reason = gv.reason
                r.guard_checks = [
                    {"name": c.name, "passed": c.passed, "detail": c.detail}
                    for c in gv.checks
                ]
                r.final_category = gv.final_category
            else:
                r.final_category = "GENUINE_MISMATCH"
        records.append(r)

    total = len(records)
    matched = sum(1 for r in records if r.matcher_status == "MATCHED")
    escalated = total - matched
    llm_reconciles = sum(1 for r in records if r.llm_reconciles is True)
    llm_declined = sum(1 for r in records if r.llm_reconciles is False)
    guard_accepted = sum(1 for r in records if r.guard_verdict == "ACCEPTED")
    guard_rejected = sum(1 for r in records if r.guard_verdict == "REJECTED")
    final_counts = Counter(r.final_category for r in records)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "total_records": total,
            "matcher_matched": matched,
            "matcher_escalated": escalated,
            "llm_says_reconciles": llm_reconciles,
            "llm_says_exception": llm_declined,
            "guard_accepted": guard_accepted,
            "guard_rejected": guard_rejected,
            "final_categories": dict(final_counts),
        },
        "records": [asdict(r) for r in records],
    }


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "0.0%"


def format_summary_text(
    report: dict,
    *,
    live_batch_source: Optional[dict] = None,
    adv_source: Optional[dict] = None,
) -> str:
    c = report["counts"]
    total = c["total_records"]
    matched = c["matcher_matched"]
    escalated = c["matcher_escalated"]
    lines = [
        "=" * 66,
        " SETTLEMENT RECONCILIATION AGENT  --  BATCH RUN",
        "=" * 66,
        "",
        "DETERMINISTIC MATCHER (src/matcher)",
        f"  total decisions       : {total}",
        f"  MATCHED               : {matched:>3}  ({_pct(matched, total)})",
        f"  ESCALATED             : {escalated:>3}  ({_pct(escalated, total)})",
        "",
        "LLM CLASSIFIER (Gemini 3.6 Flash; replay from evidence/llm_raw/)",
        f"  escalations classified: {escalated}",
        f"  LLM says reconciles   : {c['llm_says_reconciles']}",
        f"  LLM says exception    : {c['llm_says_exception']}",
        "",
        "VALIDATION GUARD (src/guard)",
        f"  accepted              : {c['guard_accepted']}",
        f"  rejected              : {c['guard_rejected']}",
        "",
        "Final category breakdown:",
    ]
    for cat, count in sorted(c["final_categories"].items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"  {cat:32s} {count}")

    # Two-label safety finding — LIVE BATCH and GUARD TEST as SEPARATE
    # facts, never collapsed. On a clean batch the guard did no work on
    # real data, but its correctness is proven against fixtures — that
    # is exactly why both lines must stand alone.
    lines += [
        "",
        "=" * 66,
        " TWO-LABEL SAFETY FINDING",
        "=" * 66,
    ]
    if live_batch_source is not None:
        lb_correct = live_batch_source["accuracy_vs_ground_truth"]["correct"]
        lb_false = live_batch_source["accuracy_vs_ground_truth"]["false_reconciliations"]
        lb_total = live_batch_source["batch_summary"]["escalated_to_llm"]
        lines.append(
            f"Live batch:  LLM produced {lb_false} false reconciliations "
            f"across {lb_total} escalations"
        )
        lines.append(
            f"             (accuracy {lb_correct}/{lb_total} vs ground truth; "
            f"source: evidence/before_guard.json)"
        )
    else:
        lines.append("Live batch:  evidence/before_guard.json not found "
                     "(run scripts/capture_before_guard.py)")
    if adv_source is not None:
        adv_rej = adv_source["counts"]["adversarial"]["rejected_by_guard"]
        adv_tot = adv_source["counts"]["adversarial"]["total"]
        lines.append(
            f"Guard test:  {adv_rej}/{adv_tot} adversarial fabrications rejected "
            f"on hand-crafted fixtures,"
        )
        lines.append(
            f"             including one arithmetically-perfect 'smart lie' "
            f"(source: evidence/after_guard.json)"
        )
    else:
        lines.append("Guard test:  evidence/after_guard.json not found "
                     "(run scripts/capture_after_guard.py)")
    lines.append("=" * 66)
    return "\n".join(lines)
