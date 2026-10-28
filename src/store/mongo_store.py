"""
Audit store — persists every reconciliation run to MongoDB.

Why a store at all: a reconciliation team needs to answer questions
after the run, not just read one report. "Which records from Tuesday's
batch were forced to GENUINE_MISMATCH?", "How many runs had a guard
rejection this week?", "Show me the full audit trail for ORD-0006."
A report printed to stdout can't answer those; a queryable store can.

Why MongoDB (a document store) rather than SQL tables: one decision's
audit trail is a nested document whose shape depends on its category.
A clean match has no LLM fields at all; an escalation carries the LLM's
claim (a free-form dict whose keys differ per category: actual_fee for
FEE_VARIANCE, refund_amount for PARTIAL_REFUND, legs for splits) and a
variable-length list of guard checks. Stored as one document it reads
back exactly as the reporter produced it. In SQL it would need separate
tables for decisions, claims and guard checks, joined on every read,
plus a JSON column for the claim anyway.

Two collections:

  runs       one document per batch: when, where it came from, counts,
             and a fingerprint of the input files
  decisions  one document per reconciliation unit, tagged with run_id

Money. The pipeline never lets a float decide whether money reconciles,
and the store keeps that promise by refusing floats outright. Decimals
are written as BSON Decimal128 (exact) and read back as decimal strings;
decimal strings are stored as they are. A float is rejected rather than
converted, because by the time a float exists its precision may already
be gone (0.1 + 0.2 is 0.30000000000000004), and converting it would only
record that error exactly. The classifier parses the LLM's JSON with
parse_float=str, so a float reaching the store means a bug upstream, and
save_run raises before writing anything.

Write order: the run document is inserted with status "writing", then
the decisions in one bulk insert, then the run is flipped to "complete".
Readers only see complete runs, so a crash halfway through a save never
shows up as a run with missing decisions. (A multi-document transaction
would also work, but needs MongoDB running as a replica set; the status
flag gives the same guarantee to readers on a single node.) Runs left in
"writing" by a crash are removed by purge_abandoned_runs, which runs
whenever the store connects.

Re-running the same input creates a new run on purpose: an audit log
records every attempt, and a re-run after a guard or prompt change can
legitimately reach different verdicts. Each run carries a fingerprint
of its input (input_fingerprint) so repeats are visible and queryable.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

from bson.decimal128 import Decimal128
from pymongo import ASCENDING, DESCENDING

from src.models import Order, Payout

DEFAULT_DB_NAME = "recon"
# A save takes seconds even for 100,000 decisions; a run still "writing"
# after this long belongs to a process that died.
ABANDONED_AFTER = timedelta(hours=1)

# Fields of a decision document that are worth returning in list views.
# The full audit trail (llm_reasoning, llm_claim, guard_checks) comes back
# from the single-decision lookup.
_DECISION_LIST_FIELDS = {
    "_id": 0,
    "run_id": 1,
    "key": 1,
    "matcher_status": 1,
    "matcher_strategy": 1,
    "llm_category": 1,
    "guard_verdict": 1,
    "guard_reason": 1,
    "final_category": 1,
}


def batch_fingerprint(orders: Iterable[Order], payouts: Iterable[Payout]) -> str:
    """SHA-256 of the batch's content, independent of row order.

    Two uploads of the same orders and payouts get the same fingerprint
    even if the CSV rows are shuffled; any changed amount, id or
    timestamp gives a different one.
    """
    def canon(rows) -> list[str]:
        return sorted("|".join(str(v) for v in vars(r).values()) for r in rows)

    h = hashlib.sha256()
    for line in canon(orders) + ["--payouts--"] + canon(payouts):
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


class AuditStore:
    """Thin layer over two MongoDB collections. Takes a pymongo Database
    (or a mongomock one in tests), so it never decides where it connects."""

    def __init__(self, db) -> None:
        self.db = db
        self.runs = db["runs"]
        self.decisions = db["decisions"]

    # ------------------------------------------------------------------
    # Setup and housekeeping
    # ------------------------------------------------------------------

    def ensure_indexes(self) -> None:
        """Create the indexes the read paths rely on. Safe to call repeatedly."""
        # Every decision query is scoped to one run, so run_id leads each index.
        # (run_id, key) is unique: one decision per record per run, and it is
        # the lookup path for "show me ORD-0006 in this run".
        self.decisions.create_index([("run_id", ASCENDING), ("key", ASCENDING)],
                                    unique=True, name="run_key")
        # "Exceptions of category X in run Y" — the reviewer's main query.
        self.decisions.create_index([("run_id", ASCENDING), ("final_category", ASCENDING)],
                                    name="run_category")
        # Newest-first run listing.
        self.runs.create_index([("created_at", DESCENDING)], name="created_at_desc")
        # "Has this exact input been reconciled before?"
        self.runs.create_index([("input_fingerprint", ASCENDING)], name="input_fingerprint")

    def purge_abandoned_runs(self, older_than: timedelta = ABANDONED_AFTER) -> int:
        """Delete runs stuck in "writing" (their process died mid-save) and
        their partial decisions. Returns how many runs were removed.

        Not a TTL index: TTL deletes documents one collection at a time, so
        it would remove the run and leave its decisions orphaned.
        """
        cutoff = datetime.now(timezone.utc) - older_than
        stale = [doc["_id"] for doc in
                 self.runs.find({"status": "writing", "created_at": {"$lt": cutoff}}, {"_id": 1})]
        if stale:
            # Children first, parent last. If this crashes in between, what is
            # left is a stale "writing" run with no decisions, which the next
            # purge finds and removes. The other order could leave decisions
            # whose run is gone, and nothing would ever find those again.
            self.decisions.delete_many({"run_id": {"$in": stale}})
            self.runs.delete_many({"_id": {"$in": stale}, "status": "writing"})
        return len(stale)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def save_run(self, report: dict, *, source: str,
                 input_fingerprint: Optional[str] = None) -> str:
        """Persist one pipeline report. Returns the new run_id.

        Raises TypeError if the report contains a float anywhere; nothing
        is written in that case.
        """
        run_id = uuid.uuid4().hex
        # Convert (and so validate) every decision before the first write, so
        # a rejected float can't leave a half-written run behind.
        documents = [_to_bson({"run_id": run_id, **record}, path=record.get("key", "?"))
                     for record in report["records"]]
        counts = _to_bson(report["counts"], path="counts")

        self.runs.insert_one({
            "_id": run_id,
            "status": "writing",
            "source": source,
            "created_at": datetime.now(timezone.utc),
            "input_fingerprint": input_fingerprint,
            "report_generated_at": report.get("generated_at"),
            "counts": counts,
        })
        if documents:
            self.decisions.insert_many(documents, ordered=False)
        self.runs.update_one({"_id": run_id}, {"$set": {"status": "complete"}})
        return run_id

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_runs(self, limit: int = 20, input_fingerprint: Optional[str] = None) -> list[dict]:
        query: dict[str, Any] = {"status": "complete"}
        if input_fingerprint is not None:
            query["input_fingerprint"] = input_fingerprint
        cursor = (self.runs.find(query)
                  .sort("created_at", DESCENDING)
                  .limit(limit))
        return [_public_run(doc) for doc in cursor]

    def count_runs_with_input(self, input_fingerprint: str) -> int:
        return self.runs.count_documents({"status": "complete",
                                          "input_fingerprint": input_fingerprint})

    def get_run(self, run_id: str) -> Optional[dict]:
        doc = self.runs.find_one({"_id": run_id, "status": "complete"})
        return _public_run(doc) if doc else None

    def find_decisions(
        self,
        run_id: str,
        *,
        category: Optional[str] = None,
        exceptions_only: bool = False,
        limit: int = 100,
        skip: int = 0,
    ) -> list[dict]:
        """Decisions for one run, optionally filtered. Sorted by record key."""
        query: dict[str, Any] = {"run_id": run_id}
        if category is not None:
            query["final_category"] = category
        elif exceptions_only:
            query["final_category"] = {"$ne": "MATCHED"}
        cursor = (self.decisions.find(query, _DECISION_LIST_FIELDS)
                  .sort("key", ASCENDING)
                  .skip(skip)
                  .limit(limit))
        return [_from_bson(doc) for doc in cursor]

    def get_decision(self, run_id: str, key: str) -> Optional[dict]:
        """The full audit trail for one record in one run."""
        doc = self.decisions.find_one({"run_id": run_id, "key": key}, {"_id": 0})
        return _from_bson(doc) if doc else None

    def category_counts(self, run_id: str) -> dict[str, int]:
        """Final-category breakdown for a run, computed by MongoDB itself."""
        pipeline = [
            {"$match": {"run_id": run_id}},
            {"$group": {"_id": "$final_category", "count": {"$sum": 1}}},
            {"$sort": {"count": -1, "_id": 1}},
        ]
        return {row["_id"]: row["count"] for row in self.decisions.aggregate(pipeline)}


# ----------------------------------------------------------------------
# Money in and out of BSON
# ----------------------------------------------------------------------

def _to_bson(value, path: str = ""):
    """Decimal becomes Decimal128, recursively. A float raises TypeError
    naming where it was found: the store never decides what a float
    "meant"."""
    if isinstance(value, float):
        raise TypeError(
            f"float {value!r} at {path!r}: money must be a Decimal or a decimal "
            f"string, never a float (its precision may already be lost)"
        )
    if isinstance(value, Decimal):
        return Decimal128(value)
    if isinstance(value, dict):
        return {k: _to_bson(v, f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_bson(v, f"{path}[{i}]") for i, v in enumerate(value)]
    return value


def _from_bson(value):
    """Decimal128 comes back as a decimal string, the same form the API and
    the LLM prompt already use for money."""
    if isinstance(value, Decimal128):
        return str(value.to_decimal())
    if isinstance(value, dict):
        return {k: _from_bson(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_bson(v) for v in value]
    return value


def _public_run(doc: dict) -> dict:
    """Shape a run document for API output (string id, ISO timestamp)."""
    created = doc["created_at"]
    if isinstance(created, datetime):
        if created.tzinfo is None:          # MongoDB hands back naive UTC
            created = created.replace(tzinfo=timezone.utc)
        created = created.isoformat()
    return {
        "run_id": doc["_id"],
        "source": doc.get("source"),
        "created_at": created,
        "input_fingerprint": doc.get("input_fingerprint"),
        "counts": doc.get("counts", {}),
    }


def store_from_env() -> Optional[AuditStore]:
    """Connect using MONGO_URL (and optional MONGO_DB). Returns None when
    MONGO_URL is unset, so the pipeline still runs with no database."""
    url = os.environ.get("MONGO_URL")
    if not url:
        return None
    from pymongo import MongoClient
    client = MongoClient(url, serverSelectionTimeoutMS=3000, tz_aware=True)
    store = AuditStore(client[os.environ.get("MONGO_DB", DEFAULT_DB_NAME)])
    store.ensure_indexes()
    store.purge_abandoned_runs()
    return store
