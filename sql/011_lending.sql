-- Money the household lends out, for instance to a company of its own.
-- Lending is not spending, and the repayment is not income: both count as
-- saving (money put away, then taken back). Only what comes back beyond
-- the amount lent counts as income: the interest.
--
-- Which payment was a loan cannot be read from the bank text, so the
-- household says so with `python -m heimkunnskap.manual lend`. Each row
-- names the payment out and a pattern for the repayments; pairing.py
-- finds the repayments after every sync and upload. The names involved
-- live only in the database, not in the code.

ALTER TABLE txn_class DROP CONSTRAINT txn_class_kind_check;
ALTER TABLE txn_class ADD CONSTRAINT txn_class_kind_check
    CHECK (kind IN ('own_transfer', 'card_payment', 'saving', 'loan', 'reimbursement',
                    'lending'));

CREATE TABLE lending (
    id          serial PRIMARY KEY,
    txn_id      bigint NOT NULL UNIQUE REFERENCES txn (id),  -- the money lent out
    repaid_by   text NOT NULL,      -- regex, any case, on counterparty and description
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE VIEW report.household_monthly AS
WITH t AS (
    SELECT date_trunc('month', t.booking_date)::date AS month,
           t.amount_minor,
           c.kind,
           c.partner_txn_id,
           coalesce(c.principal_minor, t.amount_minor) AS principal,
           coalesce(c.interest_minor, 0) + coalesce(c.fees_minor, 0) AS interest,
           a.cash_account_type = 'CARD' AS card
    FROM txn t
    JOIN account a ON a.id = t.account_id
    LEFT JOIN txn_class c ON c.txn_id = t.id
)
SELECT month,
       round(coalesce(sum(amount_minor) FILTER (WHERE kind IS NULL AND amount_minor > 0 AND NOT card), 0) / 100.0
             + coalesce(sum(interest) FILTER (WHERE kind = 'lending'), 0) / 100.0, 2) AS income,
       round(coalesce(-sum(amount_minor) FILTER (WHERE (kind IS NULL AND (amount_minor < 0 OR card))
                                                    OR kind = 'reimbursement'), 0) / 100.0, 2) AS spending,
       round(coalesce(-sum(amount_minor) FILTER (WHERE kind = 'saving'), 0) / 100.0
             + coalesce(-sum(principal) FILTER (WHERE kind = 'lending'), 0) / 100.0, 2) AS saving,
       round(coalesce(-sum(principal) FILTER (WHERE kind = 'loan'), 0) / 100.0, 2) AS loans,
       round(coalesce(sum(amount_minor) FILTER (WHERE kind IS NULL OR kind IN ('saving', 'loan', 'reimbursement', 'lending')), 0) / 100.0, 2) AS net,
       round(coalesce(sum(amount_minor) FILTER (WHERE kind IN ('own_transfer', 'card_payment')
                                               AND partner_txn_id IS NULL), 0) / 100.0, 2) AS unmatched_moves,
       count(*) AS transactions,
       round(coalesce(-sum(interest) FILTER (WHERE kind = 'loan'), 0) / 100.0, 2) AS interest
FROM t
GROUP BY month;

-- Each loan the household has made: how much is repaid, and the interest.
CREATE VIEW report.lending AS
SELECT l.id,
       t.booking_date AS lent_on,
       round(-t.amount_minor / 100.0, 2) AS lent,
       round(coalesce(sum(c.principal_minor) FILTER (WHERE c.txn_id <> l.txn_id), 0) / 100.0, 2) AS repaid,
       round(coalesce(sum(c.interest_minor), 0) / 100.0, 2) AS interest,
       round((-t.amount_minor - coalesce(sum(c.principal_minor) FILTER (WHERE c.txn_id <> l.txn_id), 0)) / 100.0, 2)
           AS outstanding,
       max(r.booking_date) AS last_repayment,
       l.note
FROM lending l
JOIN txn t ON t.id = l.txn_id
LEFT JOIN txn_class c ON c.kind = 'lending' AND (c.txn_id = l.txn_id OR c.partner_txn_id = l.txn_id)
LEFT JOIN txn r ON r.id = c.txn_id AND r.id <> l.txn_id
GROUP BY l.id, t.booking_date, t.amount_minor, l.note;
