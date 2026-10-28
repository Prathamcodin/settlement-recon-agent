"""
Shared pytest setup.

data/ is gitignored (it is regenerated from the seeded generator), so on
a fresh clone it is empty. Generate the reference batch once per test
session if it is missing, so `python -m pytest tests/` is green straight
after cloning — exactly what `python run.py` does before running.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.generator.generate import generate

DATA_DIR = Path("data")
SEED = 42  # must match the seed the committed LLM recordings were captured on


@pytest.fixture(scope="session", autouse=True)
def reference_batch() -> Path:
    if not (DATA_DIR / "orders_ledger.csv").exists() or not (DATA_DIR / "settlement_file.csv").exists():
        generate(seed=SEED, out_dir=DATA_DIR)
    return DATA_DIR
