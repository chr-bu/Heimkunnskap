-- Phase 2: card files and transfer pairing.
--
-- Card accounts from uploaded files are ordinary accounts (source 'file',
-- cash_account_type 'CARD'). A card purchase is spending; the later bill
-- payment from DNB is a move between the household's own accounts, as is
-- a transfer between two DNB accounts. txn_class marks those moves, and
-- savings and loan payments, so the reports can leave them out of spending.

ALTER TABLE txn ADD COLUMN card_holder text;  -- Amex: main or extra card
ALTER TABLE txn ADD COLUMN extra jsonb;       -- parser details: commission, country, ...

-- One row per classified transaction. Unclassified means ordinary income
-- or spending. Rows with manual = false are recomputed by
-- heimkunnskap/pairing.py after every sync and upload; manual rows are kept.
CREATE TABLE txn_class (
    txn_id          bigint PRIMARY KEY REFERENCES txn (id),
    kind            text NOT NULL
                    CHECK (kind IN ('own_transfer', 'card_payment', 'saving', 'loan')),
    partner_txn_id  bigint REFERENCES txn (id),   -- the other side, when found
    rule            text NOT NULL,                -- which rule matched, for review
    manual          boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX txn_class_partner ON txn_class (partner_txn_id);

-- DNB sends some counterparty names in 7-bit Norwegian (ISO 646-NO):
-- "L}nekassen", "Gr|nt Renhold". New rows are repaired in normalise.py.
UPDATE txn SET counterparty = translate(counterparty, '{|}[\]', 'æøåÆØÅ')
WHERE counterparty ~ '[][{|}\\]';

-- Transactions: new columns last, so existing ones and grants carry over.
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
       a.is_business_income AS business_income_account,
       t.card_holder,
       c.kind,                                    -- null: ordinary income or spending
       c.partner_txn_id AS partner_id,
       c.rule,
       t.extra
FROM txn t
JOIN account a ON a.id = t.account_id
LEFT JOIN txn_class c ON c.txn_id = t.id;

-- Per account and month. Moves between own accounts and card bill
-- payments are no longer in money_in and money_out; they are in transfers.
CREATE OR REPLACE VIEW report.monthly AS
SELECT date_trunc('month', t.booking_date)::date AS month,
       t.account_id,
       coalesce(a.label,
                CASE WHEN a.product LIKE '%,%' THEN nullif(trim(split_part(a.product, ',', 1)), '') END,
                a.product, a.name, 'account ' || a.id) AS account,
       round(coalesce(sum(t.amount_minor) FILTER (WHERE t.amount_minor > 0 AND NOT moved), 0) / 100.0, 2) AS money_in,
       round(coalesce(-sum(t.amount_minor) FILTER (WHERE t.amount_minor < 0 AND NOT moved), 0) / 100.0, 2) AS money_out,
       round(coalesce(sum(t.amount_minor) FILTER (WHERE NOT moved), 0) / 100.0, 2) AS net,
       count(*) AS transactions,
       round(coalesce(sum(t.amount_minor) FILTER (WHERE moved), 0) / 100.0, 2) AS transfers
FROM (SELECT t.*, coalesce(c.kind IN ('own_transfer', 'card_payment'), false) AS moved
      FROM txn t LEFT JOIN txn_class c ON c.txn_id = t.id) t
JOIN account a ON a.id = t.account_id
GROUP BY 1, 2, 3;

-- The household as a whole, per month. Income and spending leave out moves
-- between own accounts and card bill payments. A refund on a card reduces
-- spending rather than counting as income. Saving (fund purchases) and
-- loan payments are shown on their own. unmatched_moves is the net of
-- moves with no partner found: money moving to or from an account the
-- app cannot see, or a card bill whose card file is not uploaded yet.
CREATE VIEW report.household_monthly AS
WITH t AS (
    SELECT date_trunc('month', t.booking_date)::date AS month,
           t.amount_minor,
           c.kind,
           c.partner_txn_id,
           a.cash_account_type = 'CARD' AS card
    FROM txn t
    JOIN account a ON a.id = t.account_id
    LEFT JOIN txn_class c ON c.txn_id = t.id
)
SELECT month,
       round(coalesce(sum(amount_minor) FILTER (WHERE kind IS NULL AND amount_minor > 0 AND NOT card), 0) / 100.0, 2) AS income,
       round(coalesce(-sum(amount_minor) FILTER (WHERE kind IS NULL AND (amount_minor < 0 OR card)), 0) / 100.0, 2) AS spending,
       round(coalesce(-sum(amount_minor) FILTER (WHERE kind = 'saving'), 0) / 100.0, 2) AS saving,
       round(coalesce(-sum(amount_minor) FILTER (WHERE kind = 'loan'), 0) / 100.0, 2) AS loans,
       round(coalesce(sum(amount_minor) FILTER (WHERE kind IS NULL OR kind IN ('saving', 'loan')), 0) / 100.0, 2) AS net,
       round(coalesce(sum(amount_minor) FILTER (WHERE kind IN ('own_transfer', 'card_payment')
                                               AND partner_txn_id IS NULL), 0) / 100.0, 2) AS unmatched_moves,
       count(*) AS transactions
FROM t
GROUP BY month;

-- Moves with no partner: worth a look. A card bill whose card file is not
-- uploaded yet shows here until it is.
CREATE VIEW report.review AS
SELECT t.id,
       t.account_id,
       coalesce(a.label,
                CASE WHEN a.product LIKE '%,%' THEN nullif(trim(split_part(a.product, ',', 1)), '') END,
                a.product, a.name, 'account ' || a.id) AS account,
       t.booking_date,
       round(t.amount_minor / 100.0, 2) AS amount,
       t.description,
       c.kind,
       c.rule
FROM txn_class c
JOIN txn t ON t.id = c.txn_id
JOIN account a ON a.id = t.account_id
WHERE c.kind IN ('own_transfer', 'card_payment') AND c.partner_txn_id IS NULL;
