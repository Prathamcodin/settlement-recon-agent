"""
Audit store selection.

  DATABASE_URL set  -> PostgreSQL (src/store/postgres_store.py)
  MONGO_URL set     -> MongoDB    (src/store/mongo_store.py)
  neither           -> no store; the pipeline still runs, /runs answers 503

DATABASE_URL wins if both are set. DATABASE_SCHEMA (default "public")
picks the PostgreSQL schema.
"""
from __future__ import annotations

import os
from typing import Optional

from src.store.base import RunStore
from src.store.mongo_store import AuditStore
from src.store.mongo_store import store_from_env as mongo_store_from_env


def store_from_env() -> Optional[RunStore]:
    url = os.environ.get("DATABASE_URL")
    if url:
        from src.store.postgres_store import PostgresAuditStore
        store = PostgresAuditStore(url, schema=os.environ.get("DATABASE_SCHEMA", "public"))
        store.ensure_indexes()
        return store
    return mongo_store_from_env()


__all__ = ["AuditStore", "RunStore", "store_from_env"]
