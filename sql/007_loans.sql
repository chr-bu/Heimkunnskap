-- Loans, as confirmed 16 November 2026: a mortgage paid in instalments and
-- a credit-line mortgage (rammelån). Neither is shared through PSD2, but
-- both leave traces in the accounts:
--   - instalments: "Lån, Lån <number>, Avdrag Kr …, Renter Kr …, Omk. Kr …",
--     one a month per loan, the rammelån's with no principal;
--   - money into or out of the rammelån: "Overføring Innland, Rammelån"
--     or a move "between own accounts" whose other side is not visible.
-- heimkunnskap/pairing.py classifies these as loan and splits instalments
-- into principal, interest and fees, signed like the amount.

ALTER TABLE txn_class ADD COLUMN principal_minor bigint;
ALTER TABLE txn_class ADD COLUMN interest_minor  bigint;
ALTER TABLE txn_class ADD COLUMN fees_minor      bigint;

-- Transactions: the split, last.
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
       c.kind,
       c.partner_txn_id AS partner_id,
       c.rule,
       t.extra,
       round(c.principal_minor / 100.0, 2) AS loan_principal,
       round(c.interest_minor / 100.0, 2) AS loan_interest,
       round(c.fees_minor / 100.0, 2) AS loan_fees
FROM txn t
JOIN account a ON a.id = t.account_id
LEFT JOIN txn_class c ON c.txn_id = t.id;

-- The household per month. loans is now principal only: repaid (positive)
-- or drawn from the rammelån (negative). interest is loan interest and
-- fees, a cost of its own and later the tax deduction.
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
       round(coalesce(sum(amount_minor) FILTER (WHERE kind IS NULL AND amount_minor > 0 AND NOT card), 0) / 100.0, 2) AS income,
       round(coalesce(-sum(amount_minor) FILTER (WHERE kind IS NULL AND (amount_minor < 0 OR card)), 0) / 100.0, 2) AS spending,
       round(coalesce(-sum(amount_minor) FILTER (WHERE kind = 'saving'), 0) / 100.0, 2) AS saving,
       round(coalesce(-sum(principal) FILTER (WHERE kind = 'loan'), 0) / 100.0, 2) AS loans,
       round(coalesce(sum(amount_minor) FILTER (WHERE kind IS NULL OR kind IN ('saving', 'loan')), 0) / 100.0, 2) AS net,
       round(coalesce(sum(amount_minor) FILTER (WHERE kind IN ('own_transfer', 'card_payment')
                                               AND partner_txn_id IS NULL), 0) / 100.0, 2) AS unmatched_moves,
       count(*) AS transactions,
       round(coalesce(-sum(interest) FILTER (WHERE kind = 'loan'), 0) / 100.0, 2) AS interest
FROM t
GROUP BY month;
