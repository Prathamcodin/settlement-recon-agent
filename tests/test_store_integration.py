"""
The audit store against a real MongoDB server.

Skipped unless MONGO_URL is set, e.g.

    docker compose --profile api up -d mongo
    MONGO_URL=mongodb://localhost:27017 python -m pytest tests/test_store_integration.py

Uses its own throwaway database and drops it afterwards.
"""
from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("MONGO_URL"),
                                reason="MONGO_URL not set (no real MongoDB)")


@pytest.fixture
def real_store():
    from pymongo import MongoClient
    from src.store.mongo_store import AuditStore

    client = MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=3000, tz_aware=True)
    db_name = f"recon_it_{uuid.uuid4().hex[:8]}"
    store = AuditStore(client[db_name])
    store.ensure_indexes()
    yield store
    client.drop_database(db_name)
    client.close()


def test_round_trip_and_aggregation_on_real_mongodb(real_store):
    report = {
        "generated_at": "2026-10-28T00:00:00+00:00",
        "counts": {"total_records": 3},
        "records": [
            {"key": "ORD-1", "matcher_status": "MATCHED", "final_category": "MATCHED"},
            {"key": "ORD-2", "matcher_status": "UNMATCHED_ESCALATE", "final_category": "FEE_VARIANCE",
             "llm_claim": {"actual_fee": "3.26"}, "guard_checks": [{"name": "fee", "passed": True}]},
            {"key": "ORD-3", "matcher_status": "UNMATCHED_ESCALATE", "final_category": "GENUINE_MISMATCH"},
        ],
    }
    run_id = real_store.save_run(report, source="integration")
    assert real_store.get_run(run_id)["counts"] == {"total_records": 3}
    assert real_store.category_counts(run_id) == {"FEE_VARIANCE": 1, "GENUINE_MISMATCH": 1, "MATCHED": 1}
    assert real_store.get_decision(run_id, "ORD-2")["llm_claim"] == {"actual_fee": "3.26"}


def test_exception_query_uses_the_run_category_index(real_store):
    report = {"counts": {}, "records": [
        {"key": f"ORD-{i}", "final_category": "MATCHED" if i % 5 else "GENUINE_MISMATCH"}
        for i in range(500)
    ]}
    run_id = real_store.save_run(report, source="integration")
    plan = real_store.decisions.find(
        {"run_id": run_id, "final_category": "GENUINE_MISMATCH"}).explain()
    assert "run_category" in str(plan["queryPlanner"]["winningPlan"])


def test_duplicate_key_in_one_run_is_rejected(real_store):
    from pymongo.errors import BulkWriteError

    report = {"counts": {}, "records": [{"key": "ORD-1", "final_category": "MATCHED"},
                                        {"key": "ORD-1", "final_category": "MATCHED"}]}
    with pytest.raises(BulkWriteError):
        real_store.save_run(report, source="integration")


def test_money_is_exact_and_floats_are_rejected_on_real_mongodb(real_store):
    from decimal import Decimal
    from bson.decimal128 import Decimal128

    report = {"counts": {}, "records": [
        {"key": "ORD-1", "final_category": "FEE_VARIANCE", "llm_claim": {"actual_fee": "63.42"}},
        {"key": "ORD-2", "final_category": "PARTIAL_REFUND", "llm_claim": {"refund_amount": Decimal("0.10")}},
    ]}
    run_id = real_store.save_run(report, source="integration")
    raw = real_store.decisions.find_one({"run_id": run_id, "key": "ORD-2"})
    assert raw["llm_claim"]["refund_amount"] == Decimal128("0.10")
    assert real_store.get_decision(run_id, "ORD-2")["llm_claim"]["refund_amount"] == "0.10"

    with pytest.raises(TypeError):
        real_store.save_run({"counts": {}, "records": [
            {"key": "ORD-3", "final_category": "FEE_VARIANCE", "llm_claim": {"actual_fee": 0.1 + 0.2}},
        ]}, source="integration")
    assert real_store.runs.count_documents({"status": "writing"}) == 0


def test_purge_abandoned_runs_on_real_mongodb(real_store):
    from datetime import datetime, timedelta, timezone

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    real_store.runs.insert_one({"_id": "stale", "status": "writing", "created_at": old, "counts": {}})
    real_store.decisions.insert_one({"run_id": "stale", "key": "ORD-1", "final_category": "MATCHED"})
    assert real_store.purge_abandoned_runs() == 1
    assert real_store.decisions.count_documents({"run_id": "stale"}) == 0
