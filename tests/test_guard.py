"""
Unit tests for the validation guard.

Two layers of coverage:

  1. Parametrized over every fixture in tests/fixtures/, asserting the
     guard's accept/reject verdict matches the fixture's `expected_guard`.
     This is where the six adversarial fixtures earn their weight — each
     is an LLM-shaped response the guard MUST reject, and each positive
     control is one the guard MUST accept.

  2. Focused unit tests for the reconciles=false handling — verifying
     the guard runs the arithmetic checks even when the LLM voluntarily
     escalates, and logs the counterfactual outcome, but always accepts
     as GENUINE_MISMATCH regardless (never blindly trusting the flag).

The fixture loader lives at the top of this file. It also runs in
scripts/capture_after_guard.py; duplicated deliberately so tests don't
depend on scripts/ and vice versa.
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pytest

from src.classifier.classify import ClassifiedRecord
from src.guard.verify import Check, GuardVerdict, verify
from src.models import Order, Payout


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# --- fixture loader ------------------------------------------------------

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


def load_fixture(name: str):
    data = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    ctx = data["context"]
    resp = data["llm_response"]
    order = _decode_order(ctx.get("order"))
    in_pays = [_decode_payout(p) for p in ctx.get("in_window_payouts", [])]
    out_pays = [_decode_payout(p) for p in ctx.get("out_of_window_payouts", [])]
    record = ClassifiedRecord(
        key=ctx["key"],
        matcher_strategy=ctx.get("matcher_strategy", "amount_mismatch"),
        llm_category=resp["category"],
        llm_reconciles=bool(resp.get("reconciles", False)),
        llm_reasoning=resp.get("reasoning", ""),
        llm_claim=resp.get("claim", {}) or {},
        llm_raw_response=json.dumps(resp),
        mode="fixture",
    )
    return record, order, in_pays, out_pays, data


def _fixture_names(kind: str) -> list[str]:
    names = []
    for f in sorted(FIXTURES_DIR.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        if data.get("kind") == kind:
            names.append(f.name)
    return names


ADVERSARIAL = _fixture_names("adversarial")
POSITIVE = _fixture_names("positive")


# --- parametrized fixture tests -----------------------------------------

@pytest.mark.parametrize("fname", ADVERSARIAL)
def test_adversarial_fixture_is_rejected(fname):
    record, order, in_pays, out_pays, meta = load_fixture(fname)
    verdict = verify(record, order, in_pays, out_pays)
    assert verdict.accepted is False, (
        f"Guard FAILED to reject {fname}. Reason: {verdict.reason}\n"
        f"Checks: {[(c.name, c.passed, c.detail) for c in verdict.checks]}"
    )
    assert verdict.final_category == "GENUINE_MISMATCH"
    # Every adversarial has at least one FAILED check in the log.
    assert any(not c.passed for c in verdict.checks), (
        f"Adversarial {fname} rejected but no check was marked failed — audit gap."
    )


@pytest.mark.parametrize("fname", POSITIVE)
def test_positive_fixture_is_accepted(fname):
    record, order, in_pays, out_pays, meta = load_fixture(fname)
    verdict = verify(record, order, in_pays, out_pays)
    assert verdict.accepted is True, (
        f"Guard falsely REJECTED positive control {fname}. Reason: {verdict.reason}\n"
        f"Checks: {[(c.name, c.passed, c.detail) for c in verdict.checks]}"
    )
    # Every check that ran should have passed (or be an informational skip).
    failed = [c for c in verdict.checks if not c.passed]
    assert not failed, (
        f"Positive control {fname} accepted, but {len(failed)} check(s) failed: "
        f"{[(c.name, c.detail) for c in failed]}"
    )


# --- the "smart lie" is the pitch talking point --------------------------

def test_smart_lie_reason_names_the_specific_disagreement():
    """
    On the self-consistent-fee-lie fixture, the verdict's `reason` must call
    out that the guard rejected because the LLM's proposed number disagreed
    with the payout's actual number — not just some generic 'checks failed'
    string. That specificity is what lets the panel see the guard's teeth.
    """
    record, order, in_pays, out_pays, _ = load_fixture("adversarial_selfconsistent_fee_lie.json")
    verdict = verify(record, order, in_pays, out_pays)
    assert verdict.accepted is False
    # The reason should mention that the LLM's arithmetic would close.
    lower = verdict.reason.lower()
    assert "arithmetically" in lower or "close" in lower
    # And that it disagrees with the payout's fee_charged.
    assert "fee_charged" in verdict.reason
    assert "96.46" in verdict.reason
    assert "119.80" in verdict.reason


# --- reconciles=false always accepts but logs the counterfactual --------

def _fee_variance_record(reconciles: bool, actual_fee: str):
    return ClassifiedRecord(
        key="ORD-0006",
        matcher_strategy="amount_mismatch",
        llm_category="FEE_VARIANCE",
        llm_reconciles=reconciles,
        llm_reasoning="test",
        llm_claim={"actual_fee": actual_fee},
        llm_raw_response="",
        mode="test",
    )


def _ord_0006_ctx():
    order = Order(
        order_id="ORD-0006",
        gross_amount=Decimal("4823.00"),
        expected_fee=Decimal("96.46"),
        net_expected=Decimal("4726.54"),
        status="captured",
        created_at=datetime.fromisoformat("2026-08-20T12:00:00+00:00"),
    )
    payout = Payout(
        payout_id="PYT-0006",
        order_ref="ORD-0006",
        settled_amount=Decimal("4703.20"),
        fee_charged=Decimal("96.46"),
        settled_at=datetime.fromisoformat("2026-08-20T15:00:00+00:00"),
        batch_id="BATCH-2026-08-20",
    )
    return order, [payout], []


def test_reconciles_false_still_runs_checks_and_logs_counterfactual_fail():
    order, in_pays, out_pays = _ord_0006_ctx()
    record = _fee_variance_record(reconciles=False, actual_fee="119.80")
    verdict = verify(record, order, in_pays, out_pays)
    # ACCEPT-as-GENUINE_MISMATCH regardless of what checks said.
    assert verdict.accepted is True
    assert verdict.final_category == "GENUINE_MISMATCH"
    # But the audit trail records what the checks would have said.
    assert "would have FAILED" in verdict.reason
    assert verdict.checks, "checks list should be populated even when reconciles=False"


def test_reconciles_false_still_runs_checks_and_logs_counterfactual_pass():
    """
    A positive-fixture-shaped case: settlement internally consistent, LLM
    would have passed the FEE_VARIANCE checks, but reconciles=false. Guard
    still accepts as GENUINE_MISMATCH (LLM voluntarily escalated) — and
    the audit trail records that the checks WOULD have passed.
    """
    order = Order(
        order_id="ORD-POSFV",
        gross_amount=Decimal("1000.00"),
        expected_fee=Decimal("20.00"),
        net_expected=Decimal("980.00"),
        status="captured",
        created_at=datetime.fromisoformat("2026-08-20T10:00:00+00:00"),
    )
    payout = Payout(
        payout_id="PYT-POSFV",
        order_ref="ORD-POSFV",
        settled_amount=Decimal("976.40"),
        fee_charged=Decimal("23.60"),
        settled_at=datetime.fromisoformat("2026-08-20T14:00:00+00:00"),
        batch_id="BATCH-2026-08-20",
    )
    record = ClassifiedRecord(
        key="ORD-POSFV", matcher_strategy="amount_mismatch",
        llm_category="FEE_VARIANCE", llm_reconciles=False,
        llm_reasoning="I'm not comfortable auto-clearing this",
        llm_claim={"actual_fee": "23.60"}, llm_raw_response="", mode="test",
    )
    verdict = verify(record, order, [payout], [])
    assert verdict.accepted is True
    assert verdict.final_category == "GENUINE_MISMATCH"
    assert "would have PASSED" in verdict.reason


# --- unknown categories are rejected -------------------------------------

def test_unknown_category_is_rejected():
    order, in_pays, out_pays = _ord_0006_ctx()
    record = ClassifiedRecord(
        key="ORD-0006", matcher_strategy="amount_mismatch",
        llm_category="MADE_UP_CATEGORY", llm_reconciles=True,
        llm_reasoning="I invented this", llm_claim={}, llm_raw_response="", mode="test",
    )
    verdict = verify(record, order, in_pays, out_pays)
    assert verdict.accepted is False
    assert "Unknown" in verdict.reason
    assert any(c.name == "known_category" and not c.passed for c in verdict.checks)


# --- GENUINE_MISMATCH is always accepted regardless of context ----------

def test_genuine_mismatch_always_accepted():
    order, in_pays, out_pays = _ord_0006_ctx()
    record = ClassifiedRecord(
        key="ORD-0006", matcher_strategy="amount_mismatch",
        llm_category="GENUINE_MISMATCH", llm_reconciles=False,
        llm_reasoning="cannot classify", llm_claim={}, llm_raw_response="", mode="test",
    )
    verdict = verify(record, order, in_pays, out_pays)
    assert verdict.accepted is True
    assert verdict.final_category == "GENUINE_MISMATCH"
