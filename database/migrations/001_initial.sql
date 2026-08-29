-- 001_initial.sql
-- Purchase Milk sync layer: initial schema.
-- Run against the target PostgreSQL database once before first sync.

BEGIN;

CREATE TABLE IF NOT EXISTS purchase_milk (
    id                  BIGSERIAL PRIMARY KEY,

    guid                VARCHAR(128) NOT NULL,
    master_id           VARCHAR(64),
    alter_id            VARCHAR(64) NOT NULL,

    voucher_number      VARCHAR(64),
    date                DATE,
    voucher_type        VARCHAR(64),
    party_ledger        VARCHAR(255),

    milk_type           VARCHAR(64),
    shift               VARCHAR(32),

    litres              NUMERIC(12, 3),
    degree              NUMERIC(8, 3),
    fat                 NUMERIC(8, 3),
    snf                 NUMERIC(8, 3),

    actual_rate         NUMERIC(12, 4),
    actual_amount       NUMERIC(14, 2),

    standard_rate       VARCHAR(64),  -- raw, e.g. '74.00/ltr'
    standard_amount     NUMERIC(14, 2),

    godown              VARCHAR(128),
    "group"             VARCHAR(128),

    litres_687866861    NUMERIC(12, 3),
    litres_687866876    NUMERIC(12, 3),
    litres_687872869    NUMERIC(12, 3),
    litres_721421314    VARCHAR(64),
    litres_721421315    VARCHAR(64),

    udf_687866858       NUMERIC(12, 3),
    udf_687872868       NUMERIC(12, 3),
    udf_687872870       NUMERIC(12, 3),
    udf_553648248       VARCHAR(64),
    udf_671089661       VARCHAR(64),  -- raw, e.g. '18.26 ltr'

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_synced_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_purchase_milk_guid UNIQUE (guid)
);

CREATE INDEX IF NOT EXISTS ix_purchase_milk_date ON purchase_milk (date);
CREATE INDEX IF NOT EXISTS ix_purchase_milk_alter_id ON purchase_milk (alter_id);

CREATE TABLE IF NOT EXISTS sync_runs (
    id                  BIGSERIAL PRIMARY KEY,

    sync_type           VARCHAR(64) NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ,

    status              VARCHAR(16) NOT NULL DEFAULT 'running',

    records_fetched     INTEGER NOT NULL DEFAULT 0,
    records_inserted    INTEGER NOT NULL DEFAULT 0,
    records_updated     INTEGER NOT NULL DEFAULT 0,
    records_unchanged   INTEGER NOT NULL DEFAULT 0,
    records_failed      INTEGER NOT NULL DEFAULT 0,

    error_message       TEXT
);

CREATE INDEX IF NOT EXISTS ix_sync_runs_sync_type_started
    ON sync_runs (sync_type, started_at DESC);

COMMIT;
