"""
Audit store on PostgreSQL — same interface as the MongoDB store.

Schema (created by ensure_indexes, idempotent):

  runs       one row per batch: source, created_at, input fingerprint,
             headline counts (JSONB), and a bigserial `seq` that gives a
             strict newest-first order even when two runs share a timestamp
  decisions  one row per reconciliation unit, primary key (run_id, key).
             The fields reviewers filter on are real columns; the full
             audit trail (LLM claim, guard checks, raw rows) is one JSONB
             document, because its shape depends on the category (a
             FEE_VARIANCE claim has actual_fee, a split has leg ids).

Atomic writes. A run and all its decisions are written in one
transaction, so a crash mid-save leaves nothing behind. MongoDB needed a
"writing" status flag plus purge_abandoned_runs to give readers that
guarantee on a single node; here the database does it, and
purge_abandoned_runs has nothing to find.

Money. Floats are refused before anything is written (src.store.base.
money_safe), Decimals are kept as exact decimal strings inside JSONB, and
the order's gross amount is also a NUMERIC(14,2) column, so SQL can ask
exact questions like "exceptions over Rs.1,000" without any float.

Bulk insert uses COPY, which writes the 63-record reference batch in one
round trip and stays fast for large scale-test batches.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any, Optional

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from src.store.base import DECISION_LIST_FIELDS, money_safe

_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_DDL = """
CREATE TABLE IF NOT EXISTS {schema}.runs (
    run_id              TEXT PRIMARY KEY,
    seq                 BIGSERIAL UNIQUE,
    source              TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_fingerprint   TEXT,
    report_generated_at TEXT,
    counts              JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_input_fingerprint ON {schema}.runs (input_fingerprint);

CREATE TABLE IF NOT EXISTS {schema}.decisions (
    run_id           TEXT NOT NULL REFERENCES {schema}.runs (run_id) ON DELETE CASCADE,
    key              TEXT NOT NULL,
    matcher_status   TEXT NOT NULL,
    matcher_strategy TEXT NOT NULL,
    llm_category     TEXT,
    guard_verdict    TEXT,
    guard_reason     TEXT,
    final_category   TEXT NOT NULL,
    gross_amount     NUMERIC(14, 2),
    detail           JSONB NOT NULL,
    PRIMARY KEY (run_id, key)
);
CREATE INDEX IF NOT EXISTS decisions_run_category ON {schema}.decisions (run_id, final_category);
"""


class PostgresAuditStore:
    backend = "postgres"

    def __init__(self, url: str, *, schema: str = "public",
                 min_size: int = 1, max_size: int = 4) -> None:
        if not _SCHEMA_NAME.match(schema):
            raise ValueError(f"invalid schema name {schema!r}")
        self.schema = schema
        self.pool = ConnectionPool(
            url, min_size=min_size, max_size=max_size, open=True,
            kwargs={"row_factory": dict_row},
        )
        self._schema = sql.Identifier(schema)

    def close(self) -> None:
        self.pool.close()

    def _q(self, text: str) -> sql.Composed:
        return sql.SQL(text).format(schema=self._schema)

    # ------------------------------------------------------------------
    # Setup and housekeeping
    # ------------------------------------------------------------------

    def ensure_indexes(self) -> None:
        """Create the schema, tables and indexes. Safe to call repeatedly."""
        with self.pool.connection() as conn:
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(self._schema))
            for statement in _DDL.split(";"):
                if statement.strip():
                    conn.execute(self._q(statement))

    def purge_abandoned_runs(self, older_than: timedelta = timedelta(hours=1)) -> int:
        """Nothing to purge: saves are transactional, so no half-written
        run can exist. Kept so both stores share one interface."""
        return 0

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def save_run(self, report: dict, *, source: str,
                 input_fingerprint: Optional[str] = None) -> str:
        """Persist one pipeline report in a single transaction. Returns the
        new run_id. Raises TypeError, writing nothing, if any float is present."""
        run_id = uuid.uuid4().hex
        counts = money_safe(report["counts"], path="counts")
        rows = []
        for record in report["records"]:
            detail = money_safe(record, path=record.get("key", "?"))
            gross = (detail.get("order") or {}).get("gross_amount")
            rows.append((
                run_id, detail["key"], detail["matcher_status"], detail["matcher_strategy"],
                detail.get("llm_category"), detail.get("guard_verdict"),
                detail.get("guard_reason"), detail["final_category"],
                Decimal(gross) if gross is not None else None,
                json.dumps(detail),
            ))

        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                self._q("INSERT INTO {schema}.runs "
                        "(run_id, source, input_fingerprint, report_generated_at, counts) "
                        "VALUES (%s, %s, %s, %s, %s)"),
                (run_id, source, input_fingerprint, report.get("generated_at"), Jsonb(counts)),
            )
            with conn.cursor() as cur, cur.copy(self._q(
                "COPY {schema}.decisions (run_id, key, matcher_status, matcher_strategy, "
                "llm_category, guard_verdict, guard_reason, final_category, gross_amount, "
                "detail) FROM STDIN"
            )) as copy:
                for row in rows:
                    copy.write_row(row)
        return run_id

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    _RUN_COLUMNS = "run_id, source, created_at, input_fingerprint, counts"

    def list_runs(self, limit: int = 20, input_fingerprint: Optional[str] = None) -> list[dict]:
        where, params = "", []
        if input_fingerprint is not None:
            where, params = "WHERE input_fingerprint = %s", [input_fingerprint]
        with self.pool.connection() as conn:
            rows = conn.execute(
                self._q(f"SELECT {self._RUN_COLUMNS} FROM {{schema}}.runs {where} "
                        "ORDER BY seq DESC LIMIT %s"),
                [*params, limit],
            ).fetchall()
        return [_public_run(r) for r in rows]

    def count_runs_with_input(self, input_fingerprint: str) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                self._q("SELECT count(*) AS n FROM {schema}.runs WHERE input_fingerprint = %s"),
                [input_fingerprint],
            ).fetchone()
        return row["n"]

    def get_run(self, run_id: str) -> Optional[dict]:
        with self.pool.connection() as conn:
            row = conn.execute(
                self._q(f"SELECT {self._RUN_COLUMNS} FROM {{schema}}.runs WHERE run_id = %s"),
                [run_id],
            ).fetchone()
        return _public_run(row) if row else None

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
        where = "run_id = %s"
        params: list[Any] = [run_id]
        if category is not None:
            where += " AND final_category = %s"
            params.append(category)
        elif exceptions_only:
            where += " AND final_category <> 'MATCHED'"
        cols = ", ".join(DECISION_LIST_FIELDS)
        with self.pool.connection() as conn:
            rows = conn.execute(
                self._q(f"SELECT {cols} FROM {{schema}}.decisions WHERE {where} "
                        "ORDER BY key LIMIT %s OFFSET %s"),
                [*params, limit, skip],
            ).fetchall()
        return [dict(r) for r in rows]

    def get_decision(self, run_id: str, key: str) -> Optional[dict]:
        """The full audit trail for one record in one run."""
        with self.pool.connection() as conn:
            row = conn.execute(
                self._q("SELECT detail FROM {schema}.decisions WHERE run_id = %s AND key = %s"),
                [run_id, key],
            ).fetchone()
        return {"run_id": run_id, **row["detail"]} if row else None

    def category_counts(self, run_id: str) -> dict[str, int]:
        """Final-category breakdown for a run, computed by PostgreSQL."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                self._q("SELECT final_category, count(*) AS n FROM {schema}.decisions "
                        "WHERE run_id = %s GROUP BY final_category "
                        "ORDER BY n DESC, final_category"),
                [run_id],
            ).fetchall()
        return {r["final_category"]: r["n"] for r in rows}


def _public_run(row: dict) -> dict:
    return {
        "run_id": row["run_id"],
        "source": row["source"],
        "created_at": row["created_at"].isoformat(),
        "input_fingerprint": row["input_fingerprint"],
        "counts": row["counts"],
    }
