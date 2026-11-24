-- Readable account names. Enable Banking's account name is the holder's
-- name ("Ola Eksempel Nordmann" on nine accounts), while DNB puts the
-- household's own nickname first in the product ("Felles, BRUKSKONTO
-- TILLEGG"). Order: own label in the app, the nickname, the product, the
-- holder's name. Same columns as before, so grants carry over.

-- Accounts, with masked numbers only.
CREATE OR REPLACE VIEW report.accounts AS
SELECT a.id,
       coalesce(a.label,
                CASE WHEN a.product LIKE '%,%' THEN nullif(trim(split_part(a.product, ',', 1)), '') END,
                a.product, a.name, 'account ' || a.id) AS account,
       a.aspsp_name AS bank,
       a.product,
       a.cash_account_type,
       a.owner_name,
       a.currency,
       '…' || right(coalesce(a.bban, a.iban, ''), 4) AS number_masked,
       a.is_business_income,
       a.active,
       (SELECT max(t.booking_date) FROM txn t WHERE t.account_id = a.id) AS last_booking_date,
       (SELECT count(*) FROM txn t WHERE t.account_id = a.id) AS transactions
FROM account a;

-- Booked transactions, amounts in kroner.
CREATE OR REPLACE VIEW report.transactions AS
SELECT t.id,
       t.account_id,
       coalesce(a.label,
                CASE WHEN a.product LIKE '%,%' THEN nullif(trim(split_part(a.product, ',', 1)), '') END,
                a.product, a.name, 'account ' || a.id) AS account,
       t.booking_date,
       t.value_date,
       round(t.amount_minor / 100.0, 2) AS amount,
       t.currency,
       t.original_amount,
       t.original_currency,
       t.exchange_rate,
       t.description,
       t.counterparty,
       a.is_business_income AS business_income_account
FROM txn t
JOIN account a ON a.id = t.account_id;

-- Money in and out per account and month. Transfers between the
-- household's own accounts are still included until transfer pairing
-- arrives (phase 2), so totals across accounts overstate spending.
CREATE OR REPLACE VIEW report.monthly AS
SELECT date_trunc('month', t.booking_date)::date AS month,
       t.account_id,
       coalesce(a.label,
                CASE WHEN a.product LIKE '%,%' THEN nullif(trim(split_part(a.product, ',', 1)), '') END,
                a.product, a.name, 'account ' || a.id) AS account,
       round(sum(t.amount_minor) FILTER (WHERE t.amount_minor > 0) / 100.0, 2) AS money_in,
       round(-sum(t.amount_minor) FILTER (WHERE t.amount_minor < 0) / 100.0, 2) AS money_out,
       round(sum(t.amount_minor) / 100.0, 2) AS net,
       count(*) AS transactions
FROM txn t
JOIN account a ON a.id = t.account_id
GROUP BY 1, 2, 3;

-- Latest balance per account and balance type.
CREATE OR REPLACE VIEW report.balances AS
SELECT DISTINCT ON (b.account_id, b.balance_type)
       b.account_id,
       coalesce(a.label,
                CASE WHEN a.product LIKE '%,%' THEN nullif(trim(split_part(a.product, ',', 1)), '') END,
                a.product, a.name, 'account ' || a.id) AS account,
       b.as_of,
       b.balance_type,
       round(b.amount_minor / 100.0, 2) AS amount,
       b.currency
FROM balance b
JOIN account a ON a.id = b.account_id
ORDER BY b.account_id, b.balance_type, b.as_of DESC;

-- Imports: one row per sync of an account or uploaded file.
CREATE OR REPLACE VIEW report.imports AS
SELECT b.id,
       b.source,
       b.kind,
       coalesce(a.label,
                CASE WHEN a.product LIKE '%,%' THEN nullif(trim(split_part(a.product, ',', 1)), '') END,
                a.product, a.name, 'account ' || a.id) AS account,
       b.date_from,
       b.started_at,
       b.finished_at,
       b.pages,
       b.seen,
       b.added,
       b.error
FROM import_batch b
LEFT JOIN account a ON a.id = b.account_id;
