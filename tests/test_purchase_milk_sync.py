from __future__ import annotations

import copy

import pytest
from sqlalchemy import func, select

from database.db import get_session
from database.models import PurchaseMilk, SyncRun
from services.sync_service import sync_purchase_milk_from_records
from tests.fixtures import make_dataset, make_record

N = 10_056  # matches the client's validated live dataset size


def _db_count() -> int:
    with get_session() as s:
        return s.scalar(select(func.count()).select_from(PurchaseMilk))


def _distinct_guid_count() -> int:
    with get_session() as s:
        return s.scalar(select(func.count(func.distinct(PurchaseMilk.guid))))


def test_initial_import():
    dataset = make_dataset(N)
    result = sync_purchase_milk_from_records(dataset)

    assert result.status == "success"
    assert result.fetched == N
    assert result.inserted == N
    assert result.updated == 0
    assert result.unchanged == 0
    assert result.failed == 0

    assert _db_count() == N
    assert _distinct_guid_count() == N


def test_idempotent_sync():
    dataset = make_dataset(N)
    sync_purchase_milk_from_records(dataset)

    # Run the exact same data again.
    result = sync_purchase_milk_from_records(copy.deepcopy(dataset))

    assert result.status == "success"
    assert result.fetched == N
    assert result.inserted == 0
    assert result.updated == 0
    assert result.unchanged == N
    assert result.failed == 0
    assert _db_count() == N


def test_new_records_are_inserted_without_duplicating_existing():
    dataset = make_dataset(N)
    sync_purchase_milk_from_records(dataset)

    extra = [make_record(seq) for seq in range(N + 1, N + 11)]  # +10 new GUIDs
    result = sync_purchase_milk_from_records(dataset + extra)

    assert result.status == "success"
    assert result.fetched == N + 10
    assert result.inserted == 10
    assert result.updated == 0
    assert result.unchanged == N
    assert _db_count() == N + 10
    assert _distinct_guid_count() == N + 10


def test_alter_id_change_triggers_update_not_new_row():
    dataset = make_dataset(N)
    sync_purchase_milk_from_records(dataset)

    changed = copy.deepcopy(dataset)
    changed[0]["alter_id"] = "999999"
    changed[0]["litres"] = 12345.67  # simulate a business-field change too

    result = sync_purchase_milk_from_records(changed)

    assert result.status == "success"
    assert result.fetched == N
    assert result.inserted == 0
    assert result.updated == 1
    assert result.unchanged == N - 1
    assert _db_count() == N  # same GUID stayed one row

    with get_session() as s:
        row = s.query(PurchaseMilk).filter_by(guid=changed[0]["guid"]).one()
        assert row.alter_id == "999999"
        assert float(row.litres) == pytest.approx(12345.67)


def test_duplicate_guid_within_single_batch_does_not_create_two_rows():
    dataset = make_dataset(50)
    duplicated_batch = dataset + [dataset[0]]  # same GUID appears twice

    result = sync_purchase_milk_from_records(duplicated_batch)

    assert result.status == "success"
    assert result.inserted == 50  # deduplicated within the batch
    assert _db_count() == 50
    assert _distinct_guid_count() == 50


def test_guid_unique_constraint_enforced_at_db_level():
    dataset = make_dataset(5)
    sync_purchase_milk_from_records(dataset)

    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        with get_session() as s:
            s.add(PurchaseMilk(guid=dataset[0]["guid"], alter_id="1"))
            s.flush()


def test_transaction_rollback_on_critical_db_error_leaves_no_partial_state():
    base = make_dataset(20)
    sync_purchase_milk_from_records(base)
    assert _db_count() == 20

    runs_before = _sync_run_count()

    bad_batch = [make_record(seq) for seq in range(21, 31)]  # 10 new, valid
    # Force a DB-level failure: litres exceeds NUMERIC(12,3) precision.
    bad_batch[5]["litres"] = 10 ** 15

    result = sync_purchase_milk_from_records(base + bad_batch)

    assert result.status == "failed"
    assert result.error is not None
    # Nothing from the failed batch was written -- still exactly the
    # pre-existing 20 rows, no partial inserts from bad_batch.
    assert _db_count() == 20

    # The sync_runs audit trail still recorded the failed attempt.
    assert _sync_run_count() == runs_before + 1
    with get_session() as s:
        last_run = (
            s.query(SyncRun)
            .order_by(SyncRun.id.desc())
            .first()
        )
        assert last_run.status == "failed"
        assert last_run.records_inserted == 0
        assert last_run.records_updated == 0


def _sync_run_count() -> int:
    with get_session() as s:
        return s.scalar(select(func.count()).select_from(SyncRun))


def test_sync_runs_tracks_counts_correctly():
    dataset = make_dataset(200)
    result = sync_purchase_milk_from_records(dataset)

    with get_session() as s:
        run = s.get(SyncRun, result.sync_run_id)
        assert run.status == "success"
        assert run.records_fetched == 200
        assert run.records_inserted == 200
        assert run.records_updated == 0
        assert run.records_unchanged == 0
        assert run.records_failed == 0
        assert run.completed_at is not None
        assert run.started_at is not None
