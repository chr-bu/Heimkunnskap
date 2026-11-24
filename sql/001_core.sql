-- Core ledger: where data came from, the untouched originals, and the
-- normalised transactions. Amounts are integer øre (or the currency's
-- minor unit); a negative amount is money leaving the account.

-- One row per BankID consent (an Enable Banking session). A consent
-- covers one bank and lasts at most 180 days.
CREATE TABLE consent (
    id            bigserial PRIMARY KEY,
    session_id    text NOT NULL UNIQUE,
    aspsp_name    text NOT NULL,
    aspsp_country text NOT NULL,
    valid_until   timestamptz NOT NULL,
    status        text NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'expired', 'revoked', 'failed')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_sync_at  timestamptz,
    last_error    text,
    warned_at     timestamptz          -- last expiry warning sent
);

-- One row per real account or card account. Enable Banking gives each
-- account a new uid in every session, so an account is recognised by its
-- identification hash (stable across sessions) or, failing that, its number.
CREATE TABLE account (
    id                  bigserial PRIMARY KEY,
    account_key         text NOT NULL UNIQUE,
    source              text NOT NULL CHECK (source IN ('enable_banking', 'file')),
    aspsp_name          text,
    current_uid         text,                    -- Enable Banking uid, current session
    consent_id          bigint REFERENCES consent (id),
    name                text,                    -- as the bank names it
    label               text,                    -- the household's own name for it
    product             text,
    cash_account_type   text,
    currency            char(3) NOT NULL DEFAULT 'NOK',
    iban                text,
    bban                text,
    owner_name          text,
    is_business_income  boolean NOT NULL DEFAULT false,
    active              boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- One row per sync of one account, or per uploaded file.
CREATE TABLE import_batch (
    id           bigserial PRIMARY KEY,
    source       text NOT NULL,                  -- enable_banking, dnb_mastercard, amex
    account_id   bigint REFERENCES account (id),
    consent_id   bigint REFERENCES consent (id),
    kind         text NOT NULL,                  -- nightly, full, upload
    date_from    date,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    pages        integer NOT NULL DEFAULT 0,
    seen         integer NOT NULL DEFAULT 0,     -- booked transactions received
    added        integer NOT NULL DEFAULT 0,     -- of which new to the ledger
    error        text
);

-- The originals, untouched: each API response page or uploaded file.
-- Never edited; parsers can be fixed and re-run over these.
CREATE TABLE raw_record (
    id           bigserial PRIMARY KEY,
    batch_id     bigint NOT NULL REFERENCES import_batch (id),
    kind         text NOT NULL,                  -- transactions, balances, details, session, file
    sha256       text NOT NULL,
    payload      jsonb,                          -- API responses
    content      bytea,                          -- uploaded files
    filename     text,
    received_at  timestamptz NOT NULL DEFAULT now()
);

-- The normalised transactions. A transaction is unique by the bank's own
-- reference where there is one, otherwise by its fingerprint (account,
-- date, amount, description and an occurrence counter, so two identical
-- purchases on one day stay two rows).
CREATE TABLE txn (
    id                 bigserial PRIMARY KEY,
    account_id         bigint NOT NULL REFERENCES account (id),
    booking_date       date NOT NULL,
    value_date         date,
    amount_minor       bigint NOT NULL,          -- øre for NOK; negative = out
    currency           char(3) NOT NULL,
    original_amount    numeric(18, 4),           -- foreign purchases
    original_currency  char(3),
    exchange_rate      numeric(18, 8),
    description        text NOT NULL DEFAULT '',
    counterparty       text,
    external_ref       text,
    fingerprint        text NOT NULL,
    batch_id           bigint NOT NULL REFERENCES import_batch (id),
    raw_record_id      bigint REFERENCES raw_record (id),
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX txn_ref_uq ON txn (account_id, external_ref) WHERE external_ref IS NOT NULL;
CREATE UNIQUE INDEX txn_fp_uq ON txn (account_id, fingerprint) WHERE external_ref IS NULL;
CREATE INDEX txn_account_date ON txn (account_id, booking_date);
CREATE INDEX txn_fingerprint ON txn (fingerprint);

-- Balance snapshots, one per account, type and day.
CREATE TABLE balance (
    account_id    bigint NOT NULL REFERENCES account (id),
    as_of         date NOT NULL,
    balance_type  text NOT NULL,                 -- e.g. CLAV, ITAV, XPCD
    amount_minor  bigint NOT NULL,
    currency      char(3) NOT NULL,
    batch_id      bigint REFERENCES import_batch (id),
    PRIMARY KEY (account_id, as_of, balance_type)
);

-- Key/value settings the app keeps for itself.
CREATE TABLE app_setting (
    key    text PRIMARY KEY,
    value  text NOT NULL
);
