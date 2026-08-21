"""
Unit tests for the LLM classifier's plumbing.

These tests verify record/replay mechanics — they do NOT test whether
the LLM's classifications are correct (that is measured by
scripts/capture_before_guard.py against ground_truth.json). Nothing
here calls the real LLM API.

Note the strict split from step 4's guard tests: those tests will use
hand-crafted LLM-shaped JSON fixtures (in tests/fixtures/) to prove
the guard rejects bad claims. Those fixtures never mix with
evidence/llm_raw/. The FakeLLMBackend here is only for exercising the
record plumbing itself.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from src.classifier.backend import LLMBackend
from src.classifier.classify import (
    ClassifiedRecord,
    DEFAULT_EVIDENCE_DIR,
    build_contexts,
    classify_one,
    parse_response,
    _record_path,
)
from src.matcher.match import DEFAULT_RECON_DATE
from src.models import MatchDecision, Order, Payout


IN_WINDOW = DEFAULT_RECON_DATE - timedelta(hours=2)


def _order(order_id="ORD-1", gross="1000.00", fee="23.60", status="captured"):
    g = Decimal(gross); f = Decimal(fee)
    return Order(order_id, g, f, g - f, status, IN_WINDOW - timedelta(hours=1))


def _payout(payout_id, order_ref, settled, fee):
    return Payout(payout_id, order_ref, Decimal(settled), Decimal(fee),
                  IN_WINDOW, "BATCH-TEST")


def _escalation(order_id="ORD-1", strategy="amount_mismatch",
                matched=("PYT-1",), out_of_window=(), reason="test reason"):
    return MatchDecision(
        key=order_id,
        status="UNMATCHED_ESCALATE",
        strategy=strategy,
        matched_payout_ids=list(matched),
        out_of_window_payout_ids=list(out_of_window),
        tolerance_used=Decimal("1.00"),
        tolerance_gap=Decimal("5.00"),
        reason=reason,
    )


# --- Fake LLM backend (satisfies the LLMBackend Protocol) ---------------

class FakeLLMBackend:
    """
    Minimal in-memory backend that returns a canned response.
    Satisfies LLMBackend structurally; used only for wiring tests.
    """
    def __init__(self, response_text: str, system_prompt: str = "FAKE_SYSTEM",
                 name: str = "fake-llm"):
        self.response_text = response_text
        self.system_prompt = system_prompt
        self.name = name
        self.calls: list[dict] = []

    def classify(self, *, user: str, max_tokens: int) -> str:
        self.calls.append({"user": user, "max_tokens": max_tokens})
        return self.response_text


def test_fake_backend_satisfies_llmbackend_protocol():
    """Structural check on the Protocol — cheap smoke test of the seam."""
    fake = FakeLLMBackend("{}")
    assert isinstance(fake, LLMBackend)


# --- parse_response -----------------------------------------------------

def test_parse_response_plain_json():
    text = '{"category": "GENUINE_MISMATCH", "reconciles": false, "reasoning": "n/a", "claim": {}}'
    got = parse_response(text)
    assert got["category"] == "GENUINE_MISMATCH"
    assert got["reconciles"] is False


def test_parse_response_wrapped_in_code_fence():
    text = "```json\n" + json.dumps({"category": "FEE_VARIANCE", "reconciles": True,
                                     "reasoning": "x", "claim": {"actual_fee": "10"}}) + "\n```"
    got = parse_response(text)
    assert got["category"] == "FEE_VARIANCE"
    assert got["claim"]["actual_fee"] == "10"


def test_parse_response_wrapped_in_bare_fence():
    text = "```\n" + '{"category": "TIMING_WINDOW", "reconciles": false, "reasoning": "x", "claim": {}}' + "\n```"
    got = parse_response(text)
    assert got["category"] == "TIMING_WINDOW"


def test_parse_response_invalid_json_propagates():
    with pytest.raises(json.JSONDecodeError):
        parse_response("this is not JSON")


# --- build_contexts -----------------------------------------------------

def test_build_contexts_filters_matched_and_includes_escalations():
    o1 = _order("ORD-1")
    o2 = _order("ORD-2")
    p1 = _payout("PYT-1", "ORD-1", "976.40", "23.60")
    p2 = _payout("PYT-2", "ORD-2", "900.00", "23.60")
    matched = MatchDecision(key="ORD-1", status="MATCHED", strategy="exact",
                            matched_payout_ids=["PYT-1"])
    escalated = MatchDecision(key="ORD-2", status="UNMATCHED_ESCALATE",
                              strategy="amount_mismatch",
                              matched_payout_ids=["PYT-2"],
                              tolerance_used=Decimal("1.00"),
                              tolerance_gap=Decimal("76.40"))
    ctxs = build_contexts([matched, escalated], [o1, o2], [p1, p2])
    assert len(ctxs) == 1
    assert ctxs[0].decision.key == "ORD-2"
    assert ctxs[0].order is o2
    assert ctxs[0].in_window_payouts == [p2]


def test_build_contexts_handles_orphan_payout():
    p = _payout("PYT-9", "ORD-9999", "1000.00", "20.00")
    orphan = MatchDecision(key="ORD-9999", status="UNMATCHED_ESCALATE",
                           strategy="orphan_payout", matched_payout_ids=["PYT-9"])
    ctxs = build_contexts([orphan], [], [p])
    assert len(ctxs) == 1
    assert ctxs[0].order is None
    assert ctxs[0].in_window_payouts == [p]


# --- replay mode --------------------------------------------------------

def test_replay_reads_recorded_response(tmp_path):
    ctx = build_contexts([_escalation("ORD-42")],
                         [_order("ORD-42")],
                         [_payout("PYT-1", "ORD-42", "900.00", "23.60")])[0]
    disk_path = _record_path(tmp_path, "ORD-42")
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    disk_path.write_text(json.dumps({
        "key": "ORD-42",
        "response_text": '{"category": "GENUINE_MISMATCH", "reconciles": false, "reasoning": "no fit", "claim": {}}',
    }), encoding="utf-8")

    rec = classify_one(ctx, mode="replay", evidence_dir=tmp_path)
    assert rec.llm_category == "GENUINE_MISMATCH"
    assert rec.llm_reconciles is False
    assert rec.mode == "replay"


def test_replay_missing_response_raises(tmp_path):
    ctx = build_contexts([_escalation("ORD-42")],
                         [_order("ORD-42")],
                         [_payout("PYT-1", "ORD-42", "900.00", "23.60")])[0]
    with pytest.raises(FileNotFoundError) as ei:
        classify_one(ctx, mode="replay", evidence_dir=tmp_path)
    assert "ORD-42" in str(ei.value)
    assert "record" in str(ei.value).lower()


# --- record mode --------------------------------------------------------

def test_record_calls_backend_and_writes_verbatim(tmp_path):
    ctx = build_contexts([_escalation("ORD-77", strategy="split_incomplete",
                                      matched=["PYT-1", "PYT-2"],
                                      reason="[sum_mismatch] split candidate ...")],
                         [_order("ORD-77", gross="6300.00", fee="148.68")],
                         [_payout("PYT-1", "ORD-77", "3500.00", "80.00"),
                          _payout("PYT-2", "ORD-77", "2400.00", "100.00")])[0]

    canned_response = json.dumps({
        "category": "SPLIT_PAYOUT_WITH_VARIANCE",
        "reconciles": True,
        "reasoning": "sum off; fee variance across legs",
        "claim": {"leg_payout_ids": ["PYT-1", "PYT-2"], "fee_variance": "71.32"},
    })
    fake = FakeLLMBackend(response_text=canned_response,
                          system_prompt="FAKE_SYSTEM", name="fake-llm-v0")

    rec = classify_one(ctx, mode="record", backend=fake, evidence_dir=tmp_path)
    assert rec.llm_category == "SPLIT_PAYOUT_WITH_VARIANCE"
    assert rec.llm_reconciles is True
    assert rec.mode == "record"

    # Backend was called exactly once with the user prompt.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    from src.classifier.classify import MAX_TOKENS
    assert call["max_tokens"] == MAX_TOKENS
    assert "ORD-77" in call["user"]
    assert "PYT-1" in call["user"] and "PYT-2" in call["user"]

    # Disk record is verbatim, backend-agnostic, and carries provenance.
    disk = json.loads((tmp_path / "ORD-77.json").read_text(encoding="utf-8"))
    assert disk["key"] == "ORD-77"
    assert disk["model"] == "fake-llm-v0"
    assert disk["prompt_system"] == "FAKE_SYSTEM"
    assert disk["response_text"] == canned_response


def test_record_orphan_uses_safe_filename(tmp_path):
    """The synthetic key '<no_order_ref:PYT-1>' has characters unsafe on Windows."""
    orphan = MatchDecision(
        key="<no_order_ref:PYT-1>",
        status="UNMATCHED_ESCALATE",
        strategy="orphan_payout",
        matched_payout_ids=["PYT-1"],
    )
    ctx = build_contexts([orphan], [], [_payout("PYT-1", None, "500.00", "10.00")])[0]
    fake = FakeLLMBackend(response_text='{"category": "GENUINE_MISMATCH", "reconciles": false, "reasoning": "orphan", "claim": {}}')
    classify_one(ctx, mode="record", backend=fake, evidence_dir=tmp_path)
    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1
    assert ":" not in written[0].name
    assert "<" not in written[0].name and ">" not in written[0].name


def test_record_requires_backend(tmp_path):
    ctx = build_contexts([_escalation("ORD-1")], [_order("ORD-1")],
                         [_payout("PYT-1", "ORD-1", "900.00", "23.60")])[0]
    with pytest.raises(ValueError, match="LLM backend"):
        classify_one(ctx, mode="record", backend=None, evidence_dir=tmp_path)


def test_invalid_mode_raises(tmp_path):
    ctx = build_contexts([_escalation("ORD-1")], [_order("ORD-1")],
                         [_payout("PYT-1", "ORD-1", "900.00", "23.60")])[0]
    with pytest.raises(ValueError, match="mode must be"):
        classify_one(ctx, mode="bogus", evidence_dir=tmp_path)
