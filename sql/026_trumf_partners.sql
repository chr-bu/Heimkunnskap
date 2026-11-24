-- Trumf's partner codes, their names, and whether they are grocery stores.
-- The Groceries page counts only grocery stores. A code not listed here is
-- not counted, and the page names it, so it can be added:
--   INSERT INTO trumf_partner VALUES ('CODE', 'Name', true);
CREATE TABLE trumf_partner (
    code     text PRIMARY KEY,
    name     text NOT NULL,
    grocery  boolean NOT NULL
);

INSERT INTO trumf_partner (code, name, grocery) VALUES
    ('KIWI', 'Kiwi', true),
    ('MENY', 'Meny', true),
    ('SPAR', 'Spar and Eurospar', true),
    ('JOK', 'Joker', true),
    ('VIAT', 'Trumf Netthandel (web shops)', false),
    ('MEGR', 'Mester Grønn (florist)', false),
    ('NORL', 'Norli (books)', false),
    ('EB', 'EuroBonus (transfers)', false);

CREATE OR REPLACE VIEW report.trumf_purchases AS
SELECT p.batch_id, (p.at AT TIME ZONE 'Europe/Oslo')::date AS day, p.at, p.category, p.partner,
       p.store, round(p.amount_minor / 100.0, 2) AS amount, round(p.bonus_minor / 100.0, 2) AS bonus,
       round(p.savings_minor / 100.0, 2) AS savings, p.has_receipt,
       (SELECT count(*) FROM trumf_line l WHERE l.batch_id = p.batch_id) AS lines,
       p.txn_id, coalesce(a.label, a.name, a.product, 'account ' || a.id) AS paid_with,
       coalesce(tp.name, p.partner) AS partner_name, coalesce(tp.grocery, false) AS grocery
FROM trumf_txn p
LEFT JOIN txn t ON t.id = p.txn_id
LEFT JOIN account a ON a.id = t.account_id
LEFT JOIN trumf_partner tp ON tp.code = p.partner;

CREATE OR REPLACE VIEW report.trumf_lines AS
SELECT l.batch_id, (p.at AT TIME ZONE 'Europe/Oslo')::date AS day, p.partner, p.store, l.line_no,
       l.product, l.ean, l.quantity, l.unit, round(l.amount_minor / 100.0, 2) AS amount,
       round(l.bonus_minor / 100.0, 2) AS bonus, l.bonus_pct, l.vat_pct, l.unknown_item,
       (SELECT string_agg(r ->> 'bonusregelBeskrivelse', '; ') FROM jsonb_array_elements(l.rules) r)
           AS bonus_rules,
       coalesce(tp.grocery, false) AS grocery
FROM trumf_line l JOIN trumf_txn p ON p.batch_id = l.batch_id
LEFT JOIN trumf_partner tp ON tp.code = p.partner;
