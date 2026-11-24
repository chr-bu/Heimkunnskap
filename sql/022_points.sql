-- EuroBonus points (heimkunnskap/eurobonus.py, heimkunnskap/points.py).
--
-- The EuroBonus account's own history, from its Excel export, and the
-- points each card purchase should have earned under the card's earn rule.
-- Expected and received are compared per card and posting month; each
-- purchase is also compared with the best card that could have paid it.
-- Points are counted as points: no krone value is put on them.

CREATE TABLE eb_txn (
    id            bigserial PRIMARY KEY,
    day           date NOT NULL,
    activity      text NOT NULL,              -- as exported: "Partner | Kind"
    partner       text NOT NULL,              -- the part before " | "; flights: the route
    kind          text NOT NULL DEFAULT '',   -- Extra Points, Basic Points, Status Points, ...
    bonus_points  integer NOT NULL DEFAULT 0, -- negative: spent
    level_points  integer NOT NULL DEFAULT 0,
    fingerprint   text NOT NULL UNIQUE,       -- day, activity, points, and its place among equal rows
    batch_id      bigint NOT NULL REFERENCES import_batch (id)
);
CREATE INDEX eb_txn_day ON eb_txn (day);

-- The cards that earn EuroBonus points, how their ledger account is found
-- and what their postings are called in the EuroBonus history.
CREATE TABLE points_card (
    code             text PRIMARY KEY,
    label            text NOT NULL,
    account_pattern  text NOT NULL,           -- regex on account_key || ' ' || name
    partner          text NOT NULL,           -- EuroBonus partner name of its postings
    may_be_missing   boolean NOT NULL DEFAULT false   -- not accepted everywhere (Amex)
);

-- Earn rules, dated. Points per 100 kr in NOK and in foreign currency; a
-- yearly cap on point-earning spend; period_shift_days moves a purchase
-- into the posting month that pays for it (DNB posts around the 23rd for
-- purchases from the 25th of the month before).
CREATE TABLE earn_rule (
    id                 bigserial PRIMARY KEY,
    card               text NOT NULL REFERENCES points_card (code),
    valid_from         date NOT NULL,
    valid_to           date,
    rate_nok           numeric(6, 2) NOT NULL,
    rate_foreign       numeric(6, 2) NOT NULL,
    yearly_cap_minor   bigint,
    period_shift_days  int NOT NULL DEFAULT 0,
    note               text NOT NULL DEFAULT ''
);

-- Purchases that earn no points on any card: the bank text, lower case.
CREATE TABLE earn_exclusion (
    id       bigserial PRIMARY KEY,
    pattern  text NOT NULL,
    note     text NOT NULL DEFAULT ''
);

-- What points.py works out for each card purchase.
CREATE TABLE points_expected (
    txn_id          bigint PRIMARY KEY REFERENCES txn (id) ON DELETE CASCADE,
    card            text NOT NULL REFERENCES points_card (code),
    period          date NOT NULL,            -- the posting month it should be paid in
    earning_minor   bigint NOT NULL,          -- the amount that earns (0 if excluded or over the cap)
    foreign_cur     boolean NOT NULL,
    points          numeric(12, 2) NOT NULL,
    excluded        text,                     -- why it earns nothing
    best_card       text,
    best_points     numeric(12, 2) NOT NULL,
    merchant        text NOT NULL
);
CREATE INDEX points_expected_period ON points_expected (card, period);

INSERT INTO points_card (code, label, account_pattern, partner, may_be_missing) VALUES
    ('amex', 'American Express SAS EuroBonus', '^file:amex', 'Amex Norway Reward', true),
    ('dnb', 'DNB Mastercard (Upgrade SAGA)', '^file:dnb_mastercard', 'DNB Rewards', false),
    ('sas', 'SAS EuroBonus Mastercard', '(?i)sas eurobonus|^file:sas_mastercard', 'MasterCard Reward Norway', false);

INSERT INTO earn_rule (card, valid_from, rate_nok, rate_foreign, yearly_cap_minor, period_shift_days, note) VALUES
    ('amex', '2017-01-01', 20, 20, NULL, 3, 'stated by the cardholder, 22 November 2026'),
    ('dnb', '2017-01-01', 15, 20, 70000000, 7,
     'Mastercard Upgrade SAGA: 15 points per 100 kr, 20 in foreign currency, at most 700,000 kr a year (dnb.no)'),
    ('sas', '2017-01-01', 15, 15, NULL, 31, 'stated by the cardholder, 22 November 2026');

-- Vipps payments to people (a first and a last name, no company word) are
-- money transfers; crowdfunding too. Confirmed on the DNB card's 2026 history.
INSERT INTO earn_exclusion (pattern, note) VALUES
    ('^vipps\*(?!.*\b(as|asa|ab|sa|netthan\w*|nettapotek|apotek\w*|sport|norge|app|levert|fotball|facility|klarna|ikea|tilbords|it|robo\w*)\b)[a-zæøå]+ [a-zæøå]+',
     'Vipps to a person: a money transfer'),
    ('^vipps\*spleis', 'crowdfunding: a money transfer'),
    ('månedspris mastercard|årspris|kortavgift|\bgebyr\b|\brenter?\b', 'card fees and interest');

CREATE VIEW report.eurobonus AS
SELECT id, day, partner, kind, bonus_points, level_points, activity FROM eb_txn;

CREATE VIEW report.points_monthly AS
WITH expected AS (
    SELECT card, period, sum(-earning_minor) FILTER (WHERE earning_minor < 0) / -100.0 AS earning_spend,
           sum(points) AS expected, count(*) AS purchases,
           sum(best_points - points) AS missed
    FROM points_expected GROUP BY card, period
), received AS (
    SELECT c.code AS card, date_trunc('month', e.day)::date AS period, sum(e.bonus_points) AS received
    FROM eb_txn e JOIN points_card c ON c.partner = e.partner
    WHERE e.kind ILIKE '%points%' AND e.bonus_points <> 0
    GROUP BY 1, 2
)
SELECT coalesce(x.card, r.card) AS card, coalesce(x.period, r.period) AS period,
       round(coalesce(x.earning_spend, 0), 2) AS earning_spend,
       round(coalesce(x.expected, 0)) AS expected, coalesce(r.received, 0) AS received,
       coalesce(r.received, 0) - round(coalesce(x.expected, 0)) AS difference,
       coalesce(x.purchases, 0) AS purchases, round(coalesce(x.missed, 0)) AS missed
FROM expected x FULL JOIN received r ON r.card = x.card AND r.period = x.period;

CREATE VIEW report.points_purchases AS
SELECT t.id, t.booking_date, p.card, p.period, p.merchant, round(t.amount_minor / 100.0, 2) AS amount,
       p.foreign_cur, p.excluded, round(p.points, 1) AS points, p.best_card,
       round(p.best_points, 1) AS best_points, round(p.best_points - p.points, 1) AS missed,
       t.description
FROM points_expected p JOIN txn t ON t.id = p.txn_id;
