-- Expense claims (reiseregning) and how they change the ledger
-- (heimkunnskap/claims.py).
--
-- A claim's outlays are matched to the card payments they were; the claim
-- is matched to the salary deposit that paid it. That deposit is then split
-- in txn_part: the outlays it pays back are reimbursements, which reduce
-- spending in the category of the card payment (Travel, Car and transport),
-- and the rest (allowances less tax withheld) is income. A deposit with
-- parts is read through its parts in household_monthly and category_monthly.

CREATE TABLE expense_claim (
    id               bigserial PRIMARY KEY,
    person_id        int NOT NULL REFERENCES person (id),
    claim_no         text NOT NULL,             -- the travel system's ID
    title            text NOT NULL,             -- Utenlandsreise - Statens regulativ
    purpose          text NOT NULL DEFAULT '',
    trip_from        date NOT NULL,
    trip_to          date NOT NULL,
    total_minor      bigint NOT NULL,
    tax_free_minor   bigint NOT NULL,
    taxable_minor    bigint NOT NULL,
    ocr              boolean NOT NULL DEFAULT false,
    payout_txn_id    bigint REFERENCES txn (id) ON DELETE SET NULL,
    payout_manual    boolean NOT NULL DEFAULT false,   -- set with manual claim-payout
    raw_record_id    bigint REFERENCES raw_record (id),
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (person_id, claim_no)
);

CREATE TABLE expense_claim_line (
    id                     bigserial PRIMARY KEY,
    claim_id               bigint NOT NULL REFERENCES expense_claim (id) ON DELETE CASCADE,
    line_no                int NOT NULL,
    section                text NOT NULL CHECK (section IN ('allowance', 'cost')),
    kind                   text NOT NULL,       -- hotel, taxi, ..., per_diem, mileage
    day                    date NOT NULL,
    type_text              text NOT NULL,
    note                   text NOT NULL DEFAULT '',
    amount_minor           bigint NOT NULL,     -- NOK
    currency               text,
    currency_amount_minor  bigint,
    -- the card payment it was; match: auto, ambiguous (a guess between
    -- payments of different amounts or days), manual, or none (not by card)
    txn_id                 bigint REFERENCES txn (id) ON DELETE SET NULL,
    match                  text CHECK (match IN ('auto', 'ambiguous', 'manual', 'none')),
    UNIQUE (claim_id, line_no)
);
CREATE INDEX expense_claim_line_txn ON expense_claim_line (txn_id);

-- A transaction read as several parts. kind NULL is ordinary income or
-- spending by sign; 'reimbursement' reduces spending in category_id.
-- The parts of one transaction add up to it (claims.py builds them so).
CREATE TABLE txn_part (
    txn_id        bigint NOT NULL REFERENCES txn (id) ON DELETE CASCADE,
    part_no       int NOT NULL,
    kind          text CHECK (kind IS NULL OR kind = 'reimbursement'),
    amount_minor  bigint NOT NULL,
    category_id   bigint REFERENCES category (id),
    source        text NOT NULL,
    PRIMARY KEY (txn_id, part_no)
);

CREATE OR REPLACE VIEW report.household_monthly AS
WITH t AS (
    SELECT date_trunc('month', t.booking_date)::date AS month,
           t.id,
           coalesce(p.amount_minor, t.amount_minor) AS amount_minor,
           CASE WHEN p.txn_id IS NOT NULL THEN p.kind ELSE c.kind END AS kind,
           c.partner_txn_id,
           coalesce(c.principal_minor, p.amount_minor, t.amount_minor) AS principal,
           coalesce(c.interest_minor, 0) + coalesce(c.fees_minor, 0) AS interest,
           a.cash_account_type = 'CARD' AS card,
           c.kind = 'card_payment' AND c.partner_txn_id IS NULL AND t.amount_minor < 0
               AND a.cash_account_type IS DISTINCT FROM 'CARD' AS bill_no_detail
    FROM txn t
    JOIN account a ON a.id = t.account_id
    LEFT JOIN txn_class c ON c.txn_id = t.id
    LEFT JOIN txn_part p ON p.txn_id = t.id AND c.kind IS NULL
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
       count(DISTINCT id) AS transactions,
       round(coalesce(-sum(interest) FILTER (WHERE kind = 'loan'), 0) / 100.0, 2) AS interest,
       round(coalesce(-sum(amount_minor) FILTER (WHERE bill_no_detail), 0) / 100.0, 2) AS card_bills_no_detail
FROM t
GROUP BY month;

CREATE OR REPLACE VIEW report.category_monthly AS
SELECT month, category, sort_order,
       round(-sum(amount_minor) / 100.0, 2) AS spending,
       count(*) AS transactions
FROM (
    SELECT date_trunc('month', t.booking_date)::date AS month, cat.name AS category,
           cat.sort_order, t.amount_minor
    FROM txn_category tc
    JOIN txn t ON t.id = tc.txn_id
    JOIN category cat ON cat.id = tc.category_id
    UNION ALL
    SELECT date_trunc('month', t.booking_date)::date, cat.name, cat.sort_order, p.amount_minor
    FROM txn_part p
    JOIN txn t ON t.id = p.txn_id
    JOIN category cat ON cat.id = p.category_id
    LEFT JOIN txn_class c ON c.txn_id = t.id
    WHERE p.kind = 'reimbursement' AND c.kind IS NULL
) x
GROUP BY 1, 2, 3;

CREATE VIEW report.expense_claims AS
SELECT c.id, c.claim_no, p.name AS person, c.title, c.purpose, c.trip_from, c.trip_to,
       round(c.total_minor / 100.0, 2) AS total,
       round(c.tax_free_minor / 100.0, 2) AS tax_free,
       round(c.taxable_minor / 100.0, 2) AS taxable,
       round(coalesce(sum(l.amount_minor) FILTER (WHERE l.section = 'cost' AND l.kind IN
             ('hotel', 'flight', 'train', 'taxi', 'transit', 'parking', 'toll', 'car_hire',
              'fuel', 'other')), 0) / 100.0, 2) AS outlays,
       count(*) FILTER (WHERE l.txn_id IS NOT NULL) AS lines_matched,
       count(*) FILTER (WHERE l.match = 'ambiguous') AS lines_ambiguous,
       t.booking_date AS paid_on,
       round(t.amount_minor / 100.0, 2) AS paid
FROM expense_claim c
JOIN person p ON p.id = c.person_id
LEFT JOIN expense_claim_line l ON l.claim_id = c.id
LEFT JOIN txn t ON t.id = c.payout_txn_id
GROUP BY c.id, p.name, t.booking_date, t.amount_minor;

CREATE VIEW report.expense_claim_lines AS
SELECT l.id, c.claim_no, l.line_no, l.section, l.kind, l.day, l.type_text, l.note,
       round(l.amount_minor / 100.0, 2) AS amount, l.currency,
       round(l.currency_amount_minor / 100.0, 2) AS currency_amount,
       l.match, l.txn_id, t.booking_date AS paid_on, round(t.amount_minor / 100.0, 2) AS paid,
       t.description AS paid_text
FROM expense_claim_line l
JOIN expense_claim c ON c.id = l.claim_id
LEFT JOIN txn t ON t.id = l.txn_id;
