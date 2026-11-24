-- Reimbursed expenses ("utlegg"): money paid back for something the
-- household laid out. Not income: it reduces spending, paired with the
-- expense where heimkunnskap/pairing.py finds it.

ALTER TABLE txn_class DROP CONSTRAINT txn_class_kind_check;
ALTER TABLE txn_class ADD CONSTRAINT txn_class_kind_check
    CHECK (kind IN ('own_transfer', 'card_payment', 'saving', 'loan', 'reimbursement'));

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
       round(coalesce(-sum(amount_minor) FILTER (WHERE (kind IS NULL AND (amount_minor < 0 OR card))
                                                    OR kind = 'reimbursement'), 0) / 100.0, 2) AS spending,
       round(coalesce(-sum(amount_minor) FILTER (WHERE kind = 'saving'), 0) / 100.0, 2) AS saving,
       round(coalesce(-sum(principal) FILTER (WHERE kind = 'loan'), 0) / 100.0, 2) AS loans,
       round(coalesce(sum(amount_minor) FILTER (WHERE kind IS NULL OR kind IN ('saving', 'loan', 'reimbursement')), 0) / 100.0, 2) AS net,
       round(coalesce(sum(amount_minor) FILTER (WHERE kind IN ('own_transfer', 'card_payment')
                                               AND partner_txn_id IS NULL), 0) / 100.0, 2) AS unmatched_moves,
       count(*) AS transactions,
       round(coalesce(-sum(interest) FILTER (WHERE kind = 'loan'), 0) / 100.0, 2) AS interest
FROM t
GROUP BY month;
