"""
Tests for the MongoDB audit store (src/store/mongo_store.py).

Run against mongomock, an in-memory stand-in for MongoDB, so they need no
server. The store only talks to the pymongo collection API, so the same
code runs unchanged against a real MongoDB (see README: "Audit store").
"""
from __future__ import annotations

from pathlib import Path

import mongomock
import pytest

from src.classifier.classify import DEFAULT_EVIDENCE_DIR, classify_escalations
from src.guard.verify import verify_batch
from src.matcher.match import load_orders, load_payouts, match_batch
from src.reporter.report import build_report
from src.store.mongo_store import AuditStore


@pytest.fixture
def store() -> AuditStore:
    s = AuditStore(mongomock.MongoClient()["recon_test"])
    s.ensure_indexes()
    return s


@pytest.fixture(scope="module")
def report(reference_batch: Path) -> dict:
    """The real pipeline report for the committed 63-record batch."""
    orders = load_orders(reference_batch / "orders_ledger.csv")
    payouts = load_payouts(reference_batch / "settlement_file.csv")
    decisions = match_batch(orders, payouts)
    records = classify_escalations(decisions, orders, payouts,
                                   mode="replay", evidence_dir=DEFAULT_EVIDENCE_DIR)
    verdicts = verify_batch(records, decisions, orders, payouts)
    return build_report(decisions, records, verdicts)


def test_save_run_stores_one_decision_per_record(store, report):
    run_id = store.save_run(report, source="test")
    assert store.decisions.count_documents({"run_id": run_id}) == 63
    run = store.get_run(run_id)
    assert run["run_id"] == run_id
    assert run["source"] == "test"
    assert run["counts"]["matcher_matched"] == 51


def test_category_counts_match_the_report(store, report):
    run_id = store.save_run(report, source="test")
    # Computed by a MongoDB aggregation, so it must agree with the
    # reporter's in-memory Counter.
    assert store.category_counts(run_id) == report["counts"]["final_categories"]


def test_filter_by_category_and_exceptions(store, report):
    run_id = store.save_run(report, source="test")
    mismatches = store.find_decisions(run_id, category="GENUINE_MISMATCH")
    assert len(mismatches) == report["counts"]["final_categories"]["GENUINE_MISMATCH"]
    assert all(d["final_category"] == "GENUINE_MISMATCH" for d in mismatches)

    exceptions = store.find_decisions(run_id, exceptions_only=True)
    assert len(exceptions) == 12
    assert all(d["final_category"] != "MATCHED" for d in exceptions)


def test_get_decision_returns_full_audit_trail(store, report):
    run_id = store.save_run(report, source="test")
    d = store.get_decision(run_id, "ORD-0006")
    assert d["matcher_status"] == "UNMATCHED_ESCALATE"
    assert d["llm_category"] is not None
    assert d["guard_checks"], "the guard's per-check audit trail must be stored"
    assert "_id" not in d


def test_runs_are_isolated_and_listed_newest_first(store, report):
    first = store.save_run(report, source="first")
    second = store.save_run(report, source="second")
    assert store.decisions.count_documents({"run_id": first}) == 63
    assert store.decisions.count_documents({"run_id": second}) == 63
    assert [r["run_id"] for r in store.list_runs()] == [second, first]


def test_half_written_run_is_invisible(store, report):
    # Simulates a crash between inserting the run and finishing its decisions.
    store.runs.insert_one({"_id": "crashed", "status": "writing",
                           "source": "test", "created_at": None, "counts": {}})
    assert store.get_run("crashed") is None
    assert all(r["run_id"] != "crashed" for r in store.list_runs())


def test_unknown_run_and_key_return_none(store, report):
    run_id = store.save_run(report, source="test")
    assert store.get_run("no-such-run") is None
    assert store.get_decision(run_id, "ORD-9999") is None


def test_indexes_cover_the_read_paths(store):
    names = set(store.decisions.index_information())
    assert {"run_key", "run_category"} <= names
    assert store.decisions.index_information()["run_key"].get("unique") is True


# --------------------------------------------------------------------------
# Money is never a float, including in the database
# --------------------------------------------------------------------------

def test_decimals_and_decimal_strings_are_stored_exactly(store):
    from decimal import Decimal
    from bson.decimal128 import Decimal128

    report = {"counts": {}, "records": [
        # As the prompt asks: a decimal string, stored verbatim.
        {"key": "ORD-A", "final_category": "FEE_VARIANCE", "llm_claim": {"actual_fee": "63.42"}},
        # A Decimal computed in-process.
        {"key": "ORD-C", "final_category": "PARTIAL_REFUND",
         "llm_claim": {"refund_amount": Decimal("120.42"), "flag": True}},
    ]}
    run_id = store.save_run(report, source="test")

    raw = {d["key"]: d["llm_claim"] for d in store.decisions.find({"run_id": run_id})}
    assert raw["ORD-A"]["actual_fee"] == "63.42"
    assert raw["ORD-C"]["refund_amount"] == Decimal128("120.42")
    assert raw["ORD-C"]["flag"] is True                             # bools are not money

    # Reading back gives decimal strings, the same form the API uses.
    assert store.get_decision(run_id, "ORD-C")["llm_claim"]["refund_amount"] == "120.42"


@pytest.mark.parametrize("bad", [63.42, 0.1 + 0.2])
def test_saving_a_float_raises_and_writes_nothing(store, bad):
    report = {"counts": {}, "records": [
        {"key": "ORD-A", "final_category": "MATCHED"},
        {"key": "ORD-B", "final_category": "FEE_VARIANCE", "llm_claim": {"actual_fee": bad}},
    ]}
    with pytest.raises(TypeError, match=r"ORD-B\.llm_claim\.actual_fee"):
        store.save_run(report, source="test")
    # Rejected before the first write: no run (not even a "writing" one),
    # no decisions.
    assert store.runs.count_documents({}) == 0
    assert store.decisions.count_documents({}) == 0


def test_llm_json_numbers_are_parsed_as_exact_text_not_floats():
    # A model that ignores the prompt and returns money as a JSON number:
    # the classifier keeps the exact text, so no float is ever created.
    from src.classifier.classify import parse_response

    parsed = parse_response('{"category": "FEE_VARIANCE", "reconciles": true, '
                            '"claim": {"actual_fee": 63.42, "legs": 2}}')
    assert parsed["claim"]["actual_fee"] == "63.42"
    assert parsed["claim"]["legs"] == 2          # whole numbers are exact ints
    assert parsed["reconciles"] is True


# --------------------------------------------------------------------------
# Re-running the same input
# --------------------------------------------------------------------------

def test_fingerprint_ignores_row_order_but_not_content(reference_batch):
    import dataclasses
    from decimal import Decimal
    from src.store.mongo_store import batch_fingerprint

    orders = load_orders(reference_batch / "orders_ledger.csv")
    payouts = load_payouts(reference_batch / "settlement_file.csv")
    fp = batch_fingerprint(orders, payouts)
    assert batch_fingerprint(list(reversed(orders)), list(reversed(payouts))) == fp

    changed = [dataclasses.replace(payouts[0], settled_amount=payouts[0].settled_amount + Decimal("0.01"))]
    assert batch_fingerprint(orders, changed + payouts[1:]) != fp


def test_rerun_of_the_same_input_is_a_new_run_linked_by_fingerprint(store, report):
    first = store.save_run(report, source="test", input_fingerprint="abc")
    second = store.save_run(report, source="test", input_fingerprint="abc")
    other = store.save_run(report, source="test", input_fingerprint="xyz")
    assert first != second                                   # every attempt is audited
    assert store.count_runs_with_input("abc") == 2
    assert {r["run_id"] for r in store.list_runs(input_fingerprint="abc")} == {first, second}
    assert other not in {r["run_id"] for r in store.list_runs(input_fingerprint="abc")}


# --------------------------------------------------------------------------
# Runs abandoned mid-save
# --------------------------------------------------------------------------

def test_purge_removes_stale_writing_runs_and_their_decisions(store):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    store.runs.insert_many([
        {"_id": "stale", "status": "writing", "created_at": now - timedelta(hours=3), "counts": {}},
        {"_id": "in-progress", "status": "writing", "created_at": now - timedelta(seconds=5), "counts": {}},
    ])
    store.decisions.insert_many([
        {"run_id": "stale", "key": "ORD-1", "final_category": "MATCHED"},
        {"run_id": "in-progress", "key": "ORD-1", "final_category": "MATCHED"},
    ])

    assert store.purge_abandoned_runs() == 1
    assert store.runs.find_one({"_id": "stale"}) is None
    assert store.decisions.count_documents({"run_id": "stale"}) == 0
    # A save that may still be running is left alone.
    assert store.runs.find_one({"_id": "in-progress"}) is not None
    assert store.decisions.count_documents({"run_id": "in-progress"}) == 1


def test_purge_never_touches_complete_runs(store, report):
    from datetime import timedelta

    run_id = store.save_run(report, source="test")
    assert store.purge_abandoned_runs(older_than=timedelta(seconds=0)) == 0
    assert store.get_run(run_id) is not None


def test_purge_interrupted_between_deletes_leaves_no_orphans(store):
    """If the purge dies after deleting decisions but before deleting the
    run, the run is still a stale "writing" run, and the next purge finishes
    the job. Decisions are never left without their run."""
    from datetime import datetime, timedelta, timezone

    old = datetime.now(timezone.utc) - timedelta(hours=3)
    store.runs.insert_one({"_id": "stale", "status": "writing", "created_at": old, "counts": {}})
    store.decisions.insert_one({"run_id": "stale", "key": "ORD-1", "final_category": "MATCHED"})

    real_delete = store.runs.delete_many
    def crash(*args, **kwargs):
        raise RuntimeError("process killed")
    store.runs.delete_many = crash
    with pytest.raises(RuntimeError):
        store.purge_abandoned_runs()

    # Mid-crash state: decisions gone, run still there and still findable.
    assert store.decisions.count_documents({"run_id": "stale"}) == 0
    assert store.runs.find_one({"_id": "stale"})["status"] == "writing"

    store.runs.delete_many = real_delete
    assert store.purge_abandoned_runs() == 1
    assert store.runs.find_one({"_id": "stale"}) is None
