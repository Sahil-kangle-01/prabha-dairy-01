"""
database/migrate.py

Small convenience CLI to create the schema. For a fresh database this just
runs Base.metadata.create_all(), which produces the same schema as
migrations/001_initial.sql (that file is kept as the canonical, reviewable
SQL for production DBA use / version control).

Usage:
    python -m database.migrate
"""

from __future__ import annotations

from sqlalchemy import text

from database.db import engine
from database.models import Base, SalesVoucher, SalesInventory, AccountingEntry, Ledger, Unit, SyncCheckpoint


def run() -> None:
    Base.metadata.create_all(bind=engine)

    # Existing databases created by the first schema version had these
    # fields as NUMERIC, but Tally can return unit-bearing/text values
    # (e.g. "19.34 ltr" and "Yes"). Convert them to VARCHAR safely.
    with engine.begin() as conn:
        for column in ("litres_721421314", "litres_721421315", "udf_553648248"):
            conn.execute(text(f'ALTER TABLE purchase_milk ALTER COLUMN "{column}" TYPE VARCHAR(64) USING "{column}"::text'))

    print("Schema is up to date (purchase_milk, sync_runs).")


if __name__ == "__main__":
    run()
