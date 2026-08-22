"""
Sanity tests for the reporter — merging matcher + classifier + guard
outputs into a single report, and formatting the two-label summary.

The critical invariant for the summary: even on a clean batch (zero
false reconciliations live), the Live-batch line and the Guard-test
line must appear as SEPARATE labeled facts. A collapsed "0 -> 0"
would misrepresent the guard as idle when its correctness lives in
the adversarial fixtures, not in the live data.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from src.classifier.classify import ClassifiedRecord
from src.guard.verify import Check, GuardVerdict
from src.models import MatchDecision
from src.reporter.report import build_report, format_summary_text


def _matched(key):
    return MatchDecision(
        key=key, status="MATCHED", strategy="exact",
        matched_payout_ids=[f"PYT-{key[-4:]}"],
    )


def _escalated(key, strategy="amount_mismatch"):
    return MatchDecision(
        key=key, status="UNMATCHED_ESCALATE", strategy=strategy,
        matched_payout_ids=[f"PYT-{key[-4:]}"],
        tolerance_used=Decimal("1.00"),
        tolerance_gap=Decimal("5.00"),
        reason="test escalation",
    )


def _cr(key, category="GENUINE_MISMATCH", reconciles=False):
    return ClassifiedRecord(
        key=key, matcher_strategy="amount_mismatch",
        llm_category=category, llm_reconciles=reconciles,
        llm_reasoning="test", llm_claim={},
        llm_raw_response="", mode="test",
    )


def _v(accept=True, final="GENUINE_MISMATCH", reason="ok"):
    return GuardVerdict(accepted=accept, final_category=final, reason=reason,
                        checks=[Check("x", True, "y")])


def test_build_report_merges_by_key():
    decisions = [_matched("ORD-1"), _escalated("ORD-2")]
    classified = [_cr("ORD-2", "FEE_VARIANCE", reconciles=True)]
    verdicts = [("ORD-2", _v(accept=True, final="FEE_VARIANCE"))]
    report = build_report(decisions, classified, verdicts)
    assert report["counts"]["total_records"] == 2
    assert report["counts"]["matcher_matched"] == 1
    assert report["counts"]["matcher_escalated"] == 1
    assert report["counts"]["guard_accepted"] == 1
    assert report["counts"]["final_categories"] == {"MATCHED": 1, "FEE_VARIANCE": 1}
    keys = [r["key"] for r in report["records"]]
    assert keys == ["ORD-1", "ORD-2"]


def test_build_report_records_guard_reject():
    decisions = [_escalated("ORD-1")]
    classified = [_cr("ORD-1", "FEE_VARIANCE", reconciles=True)]
    verdicts = [("ORD-1", _v(accept=False, final="GENUINE_MISMATCH",
                             reason="settlement not internally consistent"))]
    report = build_report(decisions, classified, verdicts)
    assert report["counts"]["guard_rejected"] == 1
    r = report["records"][0]
    assert r["guard_verdict"] == "REJECTED"
    assert r["final_category"] == "GENUINE_MISMATCH"


def test_summary_prints_two_separate_labeled_facts():
    """
    On a clean batch (0 false reconciliations), Live-batch and Guard-test
    must appear as SEPARATE lines with distinct labels. Neither line may
    imply the guard did work on the live batch, and neither may imply the
    LLM produced adversarial output when it didn't.
    """
    report = build_report(
        decisions=[_matched("ORD-1"), _escalated("ORD-2")],
        classified=[_cr("ORD-2", "GENUINE_MISMATCH", reconciles=False)],
        verdicts=[("ORD-2", _v(accept=True, final="GENUINE_MISMATCH"))],
    )
    live = {
        "batch_summary": {"escalated_to_llm": 12},
        "accuracy_vs_ground_truth": {"correct": 11, "false_reconciliations": 0},
    }
    adv = {
        "counts": {"adversarial": {"total": 6, "rejected_by_guard": 6}},
    }
    text = format_summary_text(report, live_batch_source=live, adv_source=adv)

    # Both lines present, both labeled, both anchored on their own source.
    assert "Live batch:" in text
    assert "Guard test:" in text
    assert "0 false reconciliations" in text
    assert "6/6 adversarial" in text
    assert "smart lie" in text
    # The two lines are in the SAFETY FINDING block, not collapsed with each other.
    live_idx = text.index("Live batch:")
    guard_idx = text.index("Guard test:")
    assert live_idx != guard_idx
    # There is a newline between them (separate lines, not one).
    between = text[live_idx:guard_idx]
    assert "\n" in between


def test_summary_survives_missing_evidence_files():
    """If evidence artifacts aren't present, summary still prints — just
    with a 'not found' note instead of the labeled fact."""
    report = build_report([_matched("ORD-1")], [], [])
    text = format_summary_text(report, live_batch_source=None, adv_source=None)
    assert "before_guard.json not found" in text
    assert "after_guard.json not found" in text
