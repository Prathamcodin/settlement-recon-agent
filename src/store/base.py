"""
The audit-store interface both backends implement.

The API depends only on this Protocol, so the database is a deployment
choice: MongoDB (src/store/mongo_store.py) or PostgreSQL
(src/store/postgres_store.py), picked by environment variable in
src/store/__init__.py. tests/test_store_contract.py runs one suite of
behaviour tests against every backend that is available.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Optional, Protocol


class RunStore(Protocol):
    backend: str

    def ensure_indexes(self) -> None: ...
    def purge_abandoned_runs(self, older_than: timedelta = ...) -> int: ...
    def save_run(self, report: dict, *, source: str,
                 input_fingerprint: Optional[str] = None) -> str: ...
    def list_runs(self, limit: int = 20,
                  input_fingerprint: Optional[str] = None) -> list[dict]: ...
    def count_runs_with_input(self, input_fingerprint: str) -> int: ...
    def get_run(self, run_id: str) -> Optional[dict]: ...
    def find_decisions(self, run_id: str, *, category: Optional[str] = None,
                       exceptions_only: bool = False, limit: int = 100,
                       skip: int = 0) -> list[dict]: ...
    def get_decision(self, run_id: str, key: str) -> Optional[dict]: ...
    def category_counts(self, run_id: str) -> dict[str, int]: ...


# Fields returned by list views. The full audit trail (reasoning, claim,
# guard checks, raw rows) comes back from the single-decision lookup.
DECISION_LIST_FIELDS = (
    "run_id", "key", "matcher_status", "matcher_strategy", "llm_category",
    "guard_verdict", "guard_reason", "final_category",
)


def money_safe(value, path: str = ""):
    """Copy of `value` with every Decimal turned into its exact decimal
    string. A float anywhere raises TypeError naming where it was found:
    by the time a float exists its precision may already be gone, so the
    store refuses it instead of recording the error exactly."""
    if isinstance(value, float):
        raise TypeError(
            f"float {value!r} at {path!r}: money must be a Decimal or a decimal "
            f"string, never a float (its precision may already be lost)"
        )
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: money_safe(v, f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [money_safe(v, f"{path}[{i}]") for i, v in enumerate(value)]
    return value
