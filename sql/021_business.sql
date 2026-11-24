-- The sole proprietorship's expenses and its VAT per two-month term
-- (heimkunnskap/business.py).
--
-- An expense is a household payment tagged as the business's, wholly or a
-- share of it. Tagging decides: the app does not ask for receipts. Its
-- input VAT is the tagged amount's VAT at the given rate (25% unless said
-- otherwise), or an amount given by hand, and only from the VAT
-- registration date (setting vat_registered_from); before that the VAT is
-- part of the cost. Rules tag payments to a payee automatically.

CREATE TABLE business_rule (
    id          bigserial PRIMARY KEY,
    pattern     text NOT NULL,              -- as category rules: payee and text, lower case
    share       numeric(5, 4) NOT NULL DEFAULT 1 CHECK (share > 0 AND share <= 1),
    vat_rate    numeric(4, 1) NOT NULL DEFAULT 25 CHECK (vat_rate >= 0 AND vat_rate <= 25),
    note        text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE business_expense (
    txn_id      bigint PRIMARY KEY REFERENCES txn (id) ON DELETE CASCADE,
    share       numeric(5, 4) NOT NULL DEFAULT 1 CHECK (share > 0 AND share <= 1),
    vat_rate    numeric(4, 1) NOT NULL DEFAULT 25 CHECK (vat_rate >= 0 AND vat_rate <= 25),
    vat_minor   bigint,                     -- input VAT given by hand, instead of the rate
    note        text NOT NULL DEFAULT '',
    rule_id     bigint REFERENCES business_rule (id) ON DELETE CASCADE,   -- NULL: by hand
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- A deposit's VAT may belong to another term than its payment date
-- (an invoice for October paid in November): any date in that term.
ALTER TABLE business_vat ADD COLUMN term date;

-- Wholly business expenses get a category of their own, out of the
-- household's
INSERT INTO category (name, sort_order) VALUES ('Business', 980) ON CONFLICT (name) DO NOTHING;

CREATE VIEW report.business_expenses AS
WITH reg AS (
    SELECT (SELECT value FROM app_setting WHERE key = 'vat_registered_from')::date AS d
), e AS (
    SELECT t.id, t.booking_date, a.name AS account, t.description, t.amount_minor,
           e.share, e.vat_rate, e.vat_minor, e.note, r.pattern AS rule,
           round(-t.amount_minor * e.share)::bigint AS cost_minor,
           reg.d IS NOT NULL AND t.booking_date >= reg.d AS registered
    FROM business_expense e
    JOIN txn t ON t.id = e.txn_id
    JOIN account a ON a.id = t.account_id
    LEFT JOIN business_rule r ON r.id = e.rule_id
    CROSS JOIN reg
), v AS (
    SELECT *, coalesce(vat_minor, CASE WHEN registered
               THEN round(cost_minor * vat_rate / (100 + vat_rate))::bigint ELSE 0 END)
              AS input_vat_minor
    FROM e
)
SELECT id, booking_date, account, description,
       round(amount_minor / 100.0, 2) AS amount, share, vat_rate,
       round(cost_minor / 100.0, 2) AS cost,
       round(input_vat_minor / 100.0, 2) AS input_vat,
       round((cost_minor - input_vat_minor) / 100.0, 2) AS cost_excl_vat,
       CASE WHEN rule IS NULL THEN 'manual' ELSE 'rule' END AS source, rule, note,
       cost_minor, input_vat_minor
FROM v;

CREATE VIEW report.vat_terms AS
WITH reg AS (
    SELECT (SELECT value FROM app_setting WHERE key = 'vat_registered_from')::date AS d
),
-- Output VAT: deposits into the business-income account from the
-- registration date, 20% of each (25% on top of the price) unless
-- business_vat says otherwise; VAT refunds are not sales
sales AS (
    SELECT coalesce(v.term, t.booking_date) AS day, t.amount_minor,
           coalesce(v.vat_minor, t.amount_minor / 5) AS vat_minor
    FROM txn t
    JOIN account a ON a.id = t.account_id AND a.is_business_income
    LEFT JOIN txn_class c ON c.txn_id = t.id
    LEFT JOIN business_vat v ON v.txn_id = t.id
    CROSS JOIN reg
    WHERE t.amount_minor > 0 AND c.kind IS NULL AND reg.d IS NOT NULL AND t.booking_date >= reg.d
      AND t.description !~* 'Skatteetaten|Skatteinnkreving|Innkrevingsmynd'
),
costs AS (
    SELECT booking_date AS day, input_vat_minor FROM report.business_expenses
    WHERE input_vat_minor <> 0
),
-- VAT paid or refunded: a refund names its term ("2026/03" is May-June);
-- a payment belongs to the term ending before it, so the term two months
-- earlier
paid AS (
    SELECT CASE WHEN mm.m IS NOT NULL
                THEN make_date(mm.m[1]::int, mm.m[2]::int * 2 - 1, 1)
                ELSE (t.booking_date - interval '2 months')::date END AS day,
           -t.amount_minor AS paid_minor
    FROM txn t
    LEFT JOIN LATERAL (SELECT regexp_match(t.description, '(\d{4})/0?([1-6])(?!\d)') AS m) mm
         ON t.description ~* 'MVA Tilgode'
    WHERE t.description ~* 'Innkrevingsmynd\.?-?\s*Mva|MVA Tilgode'
),
items AS (
    SELECT day, vat_minor AS output_minor, 0::bigint AS input_minor, 0::bigint AS paid_minor,
           amount_minor - vat_minor AS sales_minor FROM sales
    UNION ALL SELECT day, 0, input_vat_minor, 0, 0 FROM costs
    UNION ALL SELECT day, 0, 0, paid_minor, 0 FROM paid
),
terms AS (
    SELECT generate_series(
               make_date(extract(year FROM reg.d)::int, (extract(month FROM reg.d)::int - 1) / 2 * 2 + 1, 1),
               current_date, interval '2 months')::date AS term_start
    FROM reg WHERE reg.d IS NOT NULL
)
SELECT tm.term_start,
       (tm.term_start + interval '2 months' - interval '1 day')::date AS term_end,
       CASE extract(month FROM tm.term_start)::int
            WHEN 1 THEN make_date(extract(year FROM tm.term_start)::int, 4, 10)
            WHEN 3 THEN make_date(extract(year FROM tm.term_start)::int, 6, 10)
            WHEN 5 THEN make_date(extract(year FROM tm.term_start)::int, 8, 31)
            WHEN 7 THEN make_date(extract(year FROM tm.term_start)::int, 10, 10)
            WHEN 9 THEN make_date(extract(year FROM tm.term_start)::int, 12, 10)
            ELSE make_date(extract(year FROM tm.term_start)::int + 1, 2, 10) END AS due,
       round(coalesce(sum(i.sales_minor), 0) / 100.0, 2) AS sales_excl_vat,
       round(coalesce(sum(i.output_minor), 0) / 100.0, 2) AS output_vat,
       round(coalesce(sum(i.input_minor), 0) / 100.0, 2) AS input_vat,
       round(coalesce(sum(i.output_minor) - sum(i.input_minor), 0) / 100.0, 2) AS net,
       round(coalesce(sum(i.paid_minor), 0) / 100.0, 2) AS paid,
       round(coalesce(sum(i.output_minor) - sum(i.input_minor) - sum(i.paid_minor), 0) / 100.0, 2)
           AS outstanding
FROM terms tm
LEFT JOIN items i ON i.day >= tm.term_start AND i.day < tm.term_start + interval '2 months'
GROUP BY tm.term_start;
