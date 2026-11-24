-- A card bill whose card has no data for its period (the DNB Mastercard
-- before 2026, the SAS card before its bank feed) counts as spending, in
-- its own category: the purchases behind it are real, only not itemised.
-- Before this they were left out, so years without card files looked
-- cheaper than they were. Once a card file covers the period, the bill
-- pairs with it and drops out again (pairing.py, categories.py).

INSERT INTO category (name, sort_order) VALUES ('Card purchases (no detail)', 990)
ON CONFLICT (name) DO NOTHING;

-- Holiday rentals and Emirates, seen on Amex
INSERT INTO category_rule (category_id, pattern, priority)
SELECT id, '\bvrbo\b|\bemirates\b', 40 FROM category WHERE name = 'Travel';

CREATE OR REPLACE VIEW report.household_monthly AS
WITH t AS (
    SELECT date_trunc('month', t.booking_date)::date AS month,
           t.amount_minor,
           c.kind,
           c.partner_txn_id,
           coalesce(c.principal_minor, t.amount_minor) AS principal,
           coalesce(c.interest_minor, 0) + coalesce(c.fees_minor, 0) AS interest,
           a.cash_account_type = 'CARD' AS card,
           c.kind = 'card_payment' AND c.partner_txn_id IS NULL AND t.amount_minor < 0
               AND a.cash_account_type IS DISTINCT FROM 'CARD' AS bill_no_detail
    FROM txn t
    JOIN account a ON a.id = t.account_id
    LEFT JOIN txn_class c ON c.txn_id = t.id
)
SELECT month,
       round(coalesce(sum(amount_minor) FILTER (WHERE kind IS NULL AND amount_minor > 0 AND NOT card), 0) / 100.0
             + coalesce(sum(interest) FILTER (WHERE kind = 'lending'), 0) / 100.0, 2) AS income,
       round(coalesce(-sum(amount_minor) FILTER (WHERE (kind IS NULL AND (amount_minor < 0 OR card))
                                                    OR kind = 'reimbursement' OR bill_no_detail), 0) / 100.0, 2) AS spending,
       round(coalesce(-sum(amount_minor) FILTER (WHERE kind = 'saving'), 0) / 100.0
             + coalesce(-sum(principal) FILTER (WHERE kind = 'lending'), 0) / 100.0, 2) AS saving,
       round(coalesce(-sum(principal) FILTER (WHERE kind = 'loan'), 0) / 100.0, 2) AS loans,
       round(coalesce(sum(amount_minor) FILTER (WHERE kind IS NULL OR kind IN ('saving', 'loan', 'reimbursement', 'lending')
                                                   OR bill_no_detail), 0) / 100.0, 2) AS net,
       round(coalesce(sum(amount_minor) FILTER (WHERE kind IN ('own_transfer', 'card_payment')
                                               AND partner_txn_id IS NULL AND NOT bill_no_detail), 0) / 100.0, 2) AS unmatched_moves,
       count(*) AS transactions,
       round(coalesce(-sum(interest) FILTER (WHERE kind = 'loan'), 0) / 100.0, 2) AS interest,
       round(coalesce(-sum(amount_minor) FILTER (WHERE bill_no_detail), 0) / 100.0, 2) AS card_bills_no_detail
FROM t
GROUP BY month;
