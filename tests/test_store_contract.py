"""
One behaviour suite for every audit-store backend.

The API only knows the RunStore interface (src/store/base.py), so both
backends must answer every question identically. Each test here runs
against:

  mongodb   mongomock, in memory, always
  postgres  a real PostgreSQL server, when TEST_DATABASE_URL is set, e.g.

      TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
          python -m pytest tests/test_store_contract.py

Each PostgreSQL test gets its own throwaway schema, dropped afterwards.
"""
from __future__ import annotations

import os
import uuid
from decimal import Decimal

import mongomock
import pytest

from src.api.app import _pipeline
from src.matcher.match import load_orders, load_payouts
from src.store.mongo_store import AuditStore, batch_fingerprint

PG_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(params=["mongodb", "postgres"])
def store(request):
    if request.param == "mongodb":
        s = AuditStore(mongomock.MongoClient()["recon_contract"])
        s.ensure_indexes()
        yield s
        return
    if not PG_URL:
        pytest.skip("TEST_DATABASE_URL not set (no PostgreSQL)")
    import psycopg
    from src.store.postgres_store import PostgresAuditStore
    schema = f"recon_test_{uuid.uuid4().hex[:8]}"
    s = PostgresAuditStore(PG_URL, schema=schema)
    s.ensure_indexes()
    yield s
    s.close()
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture(scope="module")
def batch(reference_batch):
    return (load_orders(reference_batch / "orders_ledger.csv"),
            load_payouts(reference_batch / "settlement_file.csv"))


@pytest.fixture(scope="module")
def report(batch) -> dict:
    """The API's report for the 63-record reference batch, raw rows included."""
    return _pipeline(*batch, store=None)


def _all(store, run_id, **kw):
    return store.find_decisions(run_id, limit=1000, **kw)


def test_save_and_read_back_a_run(store, report):
    run_id = store.save_run(report, source="test", input_fingerprint="fp")
    run = store.get_run(run_id)
    assert run["run_id"] == run_id and run["source"] == "test"
    assert run["input_fingerprint"] == "fp"
    assert run["counts"]["matcher_matched"] == 51
    assert run["created_at"]
    assert len(_all(store, run_id)) == 63


def test_category_counts_are_computed_by_the_database(store, report):
    run_id = store.save_run(report, source="test")
    assert store.category_counts(run_id) == report["counts"]["final_categories"]


def test_filters_and_ordering(store, report):
    run_id = store.save_run(report, source="test")
    fee = _all(store, run_id, category="FEE_VARIANCE")
    assert len(fee) == report["counts"]["final_categories"]["FEE_VARIANCE"]
    exceptions = _all(store, run_id, exceptions_only=True)
    assert len(exceptions) == 12
    assert all(d["final_category"] != "MATCHED" for d in exceptions)
    keys = [d["key"] for d in _all(store, run_id)]
    assert keys == sorted(keys)


def test_pagination(store, report):
    run_id = store.save_run(report, source="test")
    everything = [d["key"] for d in _all(store, run_id)]
    page = [d["key"] for d in store.find_decisions(run_id, limit=10, skip=20)]
    assert page == everything[20:30]


def test_list_view_is_slim_and_single_lookup_is_full(store, report):
    run_id = store.save_run(report, source="test")
    row = next(d for d in _all(store, run_id) if d["key"] == "ORD-0006")
    assert "guard_checks" not in row and "order" not in row
    full = store.get_decision(run_id, "ORD-0006")
    assert full["run_id"] == run_id
    assert full["guard_checks"]
    assert full["order"]["gross_amount"] == "4823.00"
    assert full["payouts"][0]["payout_id"] == "PYT-0006"
    assert "_id" not in full


def test_runs_listed_newest_first_and_isolated(store, report):
    first = store.save_run(report, source="first")
    second = store.save_run(report, source="second")
    assert [r["run_id"] for r in store.list_runs()] == [second, first]
    assert [r["run_id"] for r in store.list_runs(limit=1)] == [second]
    assert len(_all(store, first)) == len(_all(store, second)) == 63


def test_unknown_ids_return_none(store, report):
    run_id = store.save_run(report, source="test")
    assert store.get_run("nope") is None
    assert store.get_decision(run_id, "ORD-9999") is None
    assert store.find_decisions("nope") == []
    assert store.category_counts("nope") == {}


def test_reruns_are_linked_by_fingerprint(store, report, batch):
    fp = batch_fingerprint(*batch)
    assert store.count_runs_with_input(fp) == 0
    a = store.save_run(report, source="a", input_fingerprint=fp)
    b = store.save_run(report, source="b", input_fingerprint=fp)
    store.save_run(report, source="other", input_fingerprint="different")
    assert store.count_runs_with_input(fp) == 2
    assert {r["run_id"] for r in store.list_runs(input_fingerprint=fp)} == {a, b}


def _tiny_report(claim: dict) -> dict:
    return {
        "generated_at": "2026-11-27T00:00:00+00:00",
        "counts": {"total_records": 1},
        "records": [{
            "key": "ORD-X", "matcher_status": "UNMATCHED_ESCALATE",
            "matcher_strategy": "amount_mismatch", "matcher_matched_payout_ids": [],
            "matcher_reason": "", "llm_category": "FEE_VARIANCE", "llm_reconciles": True,
            "llm_reasoning": "", "llm_claim": claim, "guard_verdict": "ACCEPTED",
            "guard_reason": "", "guard_checks": [], "final_category": "FEE_VARIANCE",
            "order": None, "payouts": [],
        }],
    }


def test_money_round_trips_exactly(store):
    run_id = store.save_run(_tiny_report({"actual_fee": Decimal("0.10"),
                                          "as_text": "119.80"}), source="test")
    claim = store.get_decision(run_id, "ORD-X")["llm_claim"]
    assert claim == {"actual_fee": "0.10", "as_text": "119.80"}


def test_a_float_is_refused_and_nothing_is_written(store):
    with pytest.raises(TypeError, match="float"):
        store.save_run(_tiny_report({"actual_fee": 0.1}), source="test")
    assert store.list_runs() == []


def test_a_save_that_fails_part_way_leaves_no_visible_run(store, report):
    broken = dict(report)
    broken["records"] = report["records"] + [report["records"][0]]   # duplicate key
    with pytest.raises(Exception):
        store.save_run(broken, source="broken")
    assert store.list_runs() == []


@pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL not set")
def test_postgres_keeps_gross_amount_as_exact_numeric(report):
    """SQL can filter on money exactly: the order's gross is NUMERIC(14,2)."""
    import psycopg
    from src.store.postgres_store import PostgresAuditStore
    schema = f"recon_test_{uuid.uuid4().hex[:8]}"
    s = PostgresAuditStore(PG_URL, schema=schema)
    try:
        s.ensure_indexes()
        run_id = s.save_run(report, source="test")
        with s.pool.connection() as conn:
            row = conn.execute(
                f'SELECT gross_amount FROM "{schema}".decisions WHERE run_id = %s AND key = %s',
                [run_id, "ORD-0006"]).fetchone()
        assert row["gross_amount"] == Decimal("4823.00")
    finally:
        s.close()
        with psycopg.connect(PG_URL, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
