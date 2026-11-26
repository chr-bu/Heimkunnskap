-- Spending milestones per card and calendar year: what a card gives when its
-- spending passes an amount (level points, a 2-for-1 voucher). Shown as
-- progress on the Points page. Terms change: each row is dated, and a new
-- row with a later valid_from replaces it. Set your own with SQL or
-- manual milestone (heimkunnskap/manual.py).
CREATE TABLE points_milestone (
    id           serial PRIMARY KEY,
    card         text NOT NULL REFERENCES points_card (code),
    spend_minor  bigint NOT NULL,             -- the year's earning spend that reaches it
    reward       text NOT NULL,               -- what it gives, in a few words
    level_points integer NOT NULL DEFAULT 0,  -- level points it gives, if any
    valid_from   date NOT NULL,
    valid_to     date
);

INSERT INTO points_milestone (card, spend_minor, reward, level_points, valid_from) VALUES
    -- DNB Mastercard Upgrade SAGA (dnb.no, terms MU012026): level points
    -- per calendar year of point-earning spend
    ('dnb', 10000000, '10 000 level points', 10000, '2024-01-01'),
    ('dnb', 20000000, '10 000 level points', 10000, '2024-01-01'),
    ('dnb', 50000000, '10 000 level points', 10000, '2024-01-01'),
    -- SAS Amex Elite (americanexpress.com/no): a 2-for-1 voucher per 150 000 kr
    -- in a calendar year, two at most; its 6 level points per 100 kr come
    -- continuously and need no milestone
    ('amex', 15000000, '2-for-1 voucher', 0, '2024-01-01'),
    ('amex', 30000000, 'second 2-for-1 voucher', 0, '2024-01-01');
