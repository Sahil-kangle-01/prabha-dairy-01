"""
database/models.py

SQLAlchemy models for the Purchase Milk sync layer.

Design notes:
  - `guid` is the business identity of a voucher and carries a UNIQUE
    constraint -- this is what sync_service.py upserts against.
  - `alter_id` is the change-detection field: same GUID + same alter_id
    means "unchanged", different alter_id means "changed, needs UPDATE".
  - Numeric fields the parser already gives us clean (litres, degree, fat,
    snf, rates/amounts, litres_*/udf_* numeric columns) are stored as
    NUMERIC. `standard_rate`, `udf_671089661`, `litres_721421314`, `litres_721421315`,
    and `udf_553648248` are stored as raw String because the source values
    can carry unit suffixes or text such as `Yes` ("74.00/ltr",
    "18.26 ltr") that would be lossy to strip.
  - `group` is a SQL reserved word; the Python attribute is named
    `group_` but the actual column name is `group`, matching the schema
    the client asked for.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    JSON,
    Column,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PurchaseMilk(Base):
    __tablename__ = "purchase_milk"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    guid = Column(String(128), nullable=False, unique=True, index=True)
    master_id = Column(String(64))
    alter_id = Column(String(64), nullable=False)

    voucher_number = Column(String(64))
    date = Column(Date)
    voucher_type = Column(String(64))
    party_ledger = Column(String(255))

    milk_type = Column(String(64))
    shift = Column(String(32))

    litres = Column(Numeric(12, 3))
    degree = Column(Numeric(8, 3))
    fat = Column(Numeric(8, 3))
    snf = Column(Numeric(8, 3))

    actual_rate = Column(Numeric(12, 4))
    actual_amount = Column(Numeric(14, 2))

    standard_rate = Column(String(64))  # raw, e.g. "74.00/ltr"
    standard_amount = Column(Numeric(14, 2))

    godown = Column(String(128))
    group = Column("group", String(128))

    litres_687866861 = Column(Numeric(12, 3))
    litres_687866876 = Column(Numeric(12, 3))
    litres_687872869 = Column(Numeric(12, 3))
    litres_721421314 = Column(String(64))  # raw Tally value, may include unit text
    litres_721421315 = Column(String(64))  # raw Tally value, may include unit text

    udf_687866858 = Column(Numeric(12, 3))
    udf_687872868 = Column(Numeric(12, 3))
    udf_687872870 = Column(Numeric(12, 3))
    udf_553648248 = Column(String(64))  # raw Tally value, e.g. "Yes"
    udf_671089661 = Column(String(64))  # raw, e.g. "18.26 ltr"

    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
    last_synced_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("guid", name="uq_purchase_milk_guid"),
        Index("ix_purchase_milk_date", "date"),
        Index("ix_purchase_milk_alter_id", "alter_id"),
    )


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    sync_type = Column(String(64), nullable=False)  # e.g. "purchase_milk"
    started_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # running | success | failed
    status = Column(String(16), nullable=False, default="running")

    records_fetched = Column(Integer, nullable=False, default=0)
    records_inserted = Column(Integer, nullable=False, default=0)
    records_updated = Column(Integer, nullable=False, default=0)
    records_unchanged = Column(Integer, nullable=False, default=0)
    records_failed = Column(Integer, nullable=False, default=0)

    error_message = Column(Text, nullable=True)

class StockItem(Base):
    __tablename__ = "stock_items"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Tally identity
    name = Column(String(255), nullable=False, unique=True, index=True)

    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class Godown(Base):
    __tablename__ = "godowns"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Tally godown name
    name = Column(String(255), nullable=False, unique=True, index=True)

    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Tally voucher identity
    guid = Column(String(128), nullable=False)
    master_id = Column(String(64))
    alter_id = Column(String(64), nullable=False)

    voucher_date = Column(Date, nullable=False)
    voucher_number = Column(String(64))
    voucher_type = Column(String(64))

    # Stock information
    stock_item = Column(String(255), nullable=False, index=True)

    quantity = Column(Numeric(14, 3))
    unit = Column(String(32))

    rate = Column(Numeric(14, 4))
    amount = Column(Numeric(16, 2))

    # Godown movement
    source_godown = Column(String(255))
    destination_godown = Column(String(255))

    # Examples:
    # SALE, PURCHASE, STOCK_JOURNAL_IN, STOCK_JOURNAL_OUT,
    # GODOWN_TRANSFER, ADJUSTMENT
    movement_type = Column(String(64), nullable=False, index=True)

    batch_name = Column(String(255))

    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )

    __table_args__ = (
        Index("ix_stock_movements_date", "voucher_date"),
        Index("ix_stock_movements_guid", "guid"),
        Index("ix_stock_movements_item_date", "stock_item", "voucher_date"),
        Index(
            "ix_stock_movements_item_godown",
            "stock_item",
            "source_godown",
        ),
    )# Sync Now model additions
# Add Boolean/ForeignKey/JSON to the existing sqlalchemy import block:
#     Boolean, ForeignKey, JSON
#
# Then append everything below AFTER the existing StockMovement model.
# Do not remove or modify the existing models.

class SalesVoucher(Base):
    __tablename__ = "sales_vouchers"

    id = Column(Integer, primary_key=True)
    guid = Column(String(128), nullable=False, unique=True, index=True)
    master_id = Column(BigInteger, nullable=True, index=True)
    alter_id = Column(BigInteger, nullable=True, index=True)
    voucher_date = Column(Date, nullable=True, index=True)
    voucher_number = Column(String(128), nullable=True)
    voucher_type = Column(String(128), nullable=True)
    party_ledger = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class SalesInventory(Base):
    __tablename__ = "sales_inventory"

    id = Column(Integer, primary_key=True)
    sales_voucher_id = Column(Integer, ForeignKey("sales_vouchers.id", ondelete="CASCADE"),
                              nullable=False, index=True)

    stock_item = Column(String(255), nullable=True, index=True)
    quantity = Column(Numeric(18, 6), nullable=True)
    unit = Column(String(64), nullable=True)
    billed_quantity = Column(String(128), nullable=True)
    rate = Column(Numeric(18, 6), nullable=True)
    amount = Column(Numeric(18, 6), nullable=True)
    source_godown = Column(String(255), nullable=True, index=True)
    destination_godown = Column(String(255), nullable=True, index=True)
    batch_name = Column(String(255), nullable=True)
    is_deemed_positive = Column(String(32), nullable=True)
    movement_type = Column(String(32), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_sales_inventory_voucher_item", "sales_voucher_id", "stock_item"),
    )


class AccountingEntry(Base):
    __tablename__ = "accounting_entries"

    id = Column(Integer, primary_key=True)
    sales_voucher_id = Column(Integer, ForeignKey("sales_vouchers.id", ondelete="CASCADE"),
                              nullable=False, index=True)

    guid = Column(String(128), nullable=False, index=True)
    master_id = Column(BigInteger, nullable=True, index=True)
    alter_id = Column(BigInteger, nullable=True, index=True)
    voucher_date = Column(Date, nullable=True, index=True)
    voucher_number = Column(String(128), nullable=True)
    voucher_type = Column(String(128), nullable=True)
    party_ledger = Column(String(255), nullable=True)
    reference = Column(String(255), nullable=True)
    is_invoice = Column(String(32), nullable=True)
    ledger_name = Column(String(255), nullable=True, index=True)
    amount = Column(Numeric(18, 6), nullable=True)
    is_deemed_positive = Column(String(32), nullable=True)
    is_party_ledger = Column(String(32), nullable=True)
    ledger_from_item = Column(String(32), nullable=True)
    bill_reference = Column(String(255), nullable=True)
    bill_date = Column(String(64), nullable=True)
    bill_type = Column(String(128), nullable=True)
    cost_centre = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_accounting_entries_voucher_ledger", "sales_voucher_id", "ledger_name"),
    )


class Ledger(Base):
    __tablename__ = "ledgers"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False, unique=True, index=True)
    parent = Column(String(255), nullable=True)
    guid = Column(String(128), nullable=True, index=True)
    opening_balance = Column(Numeric(18, 6), nullable=True)
    closing_balance = Column(Numeric(18, 6), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class Unit(Base):
    __tablename__ = "units"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False, unique=True, index=True)

    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class SyncCheckpoint(Base):
    __tablename__ = "sync_checkpoints"

    id = Column(Integer, primary_key=True)
    sync_type = Column(String(64), nullable=False, unique=True, index=True)
    last_alter_id = Column(BigInteger, nullable=True, index=True)
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(32), nullable=True)
    metadata_json = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

