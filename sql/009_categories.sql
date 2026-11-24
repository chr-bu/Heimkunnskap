-- Spending categories. Rules match the lower-cased counterparty and
-- description of an ordinary purchase or payment (not a move between own
-- accounts, a card bill, saving or a loan) with a Python regular
-- expression; the first match by priority wins, and anything unmatched is
-- Other. Edited on the /categories page; heimkunnskap/categories.py
-- recomputes txn_category after every sync, upload and rule change.
-- Seeded 16 November 2026 from the first grouping of the household's data.

CREATE TABLE category (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    sort_order  int NOT NULL DEFAULT 100
);

CREATE TABLE category_rule (
    id           bigserial PRIMARY KEY,
    category_id  bigint NOT NULL REFERENCES category (id),
    pattern      text NOT NULL,
    priority     int NOT NULL DEFAULT 100,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE txn_category (
    txn_id       bigint PRIMARY KEY REFERENCES txn (id),
    category_id  bigint NOT NULL REFERENCES category (id),
    rule_id      bigint REFERENCES category_rule (id) ON DELETE SET NULL
);
CREATE INDEX txn_category_category ON txn_category (category_id);

INSERT INTO category (name, sort_order) VALUES
    ('Tax', 0),
    ('Groceries', 10),
    ('Wine and spirits', 20),
    ('Travel', 30),
    ('Insurance', 40),
    ('Home, utilities and cabin', 50),
    ('Phone and internet', 60),
    ('Car and transport', 70),
    ('Eating out', 80),
    ('Health and fitness', 90),
    ('Card and bank fees', 100),
    ('Subscriptions and media', 110),
    ('Sport, culture and leisure', 120),
    ('Dog', 130),
    ('Children', 140),
    ('Charity', 150),
    ('Shopping, clothes and electronics', 160),
    ('Other', 170);

INSERT INTO category_rule (category_id, pattern, priority)
SELECT c.id, r.pattern, r.priority FROM (VALUES
    -- A starting set of Norwegian payees, first match by priority. Each
    -- household adds its own (manual category-rule, or SQL).
    ('Tax', 'innkrevingsmynd|skatteetaten|skatteinnkreving', 10),
    ('Groceries', '\bkiwi\b|\bmeny\b|rema 1000|\bcoop\b|\bextra\b|\bspar\b|eurospar|\bjoker\b|\bcentra\b|bunnpris|norgesgruppen trumf|godtlevert|oda\.com|kokkeloeren', 20),
    ('Wine and spirits', 'vinmonopolet|duty-free', 30),
    ('Travel', '\bhotel|hotell|agoda|airbnb|flysas|scandinavian airline|wideroe|widerøe|norwegian air|awardfares|getyourguide|holafly|\buber\b|\btaxi|booking\.com|hotels\.com', 40),
    ('Insurance', 'if skadeforsikring|fremtind|\bagria\b|gjensidige|\btryg\b', 50),
    ('Home, utilities and cabin', 'tibber|fjordkraft|\bkommune\b|sector alarm|verisure|rørlegger|rorlegger|maxbo|flugger|clas ?ohl|\bikea\b|jernia|byggmax|mester gr[øo]nn|obs bygg|montér', 60),
    ('Phone and internet', 'globalconnect|starlink|telenor|telia|ice\.net|altibox', 70),
    ('Car and transport', '\bruter|\bvy\b|\bbilia|vegvese|bilvask|circle k|\besso\b|\bshell\b|\buno-x|easypark|bompenger|fjellinjen|autopass|parkering|\btesla\b|mer norway|\brecharge\b', 80),
    ('Eating out', 'kaffebrenneriet|bakeri|peppes|restaurant|seven-eleven|7-eleven|narvesen|starbucks|espresso house|deli de luca|burger|sushi|pizza|foodora|wolt', 90),
    ('Health and fitness', 'farmasiet|apotek|vitusapotek|\btannl|legesen|legekontor|\bsats\b|evo fitness|klinikk|optiker|fysio', 100),
    ('Card and bank fees', 'medlemsavgift|mastercard upgrade|år-/mån avg|årsavgift|kortavgift|prislagte tjenester', 110),
    ('Subscriptions and media', 'spotify|youtube|disney|hbo|\bmax\.com|netflix|viaplay|dagens næringsliv|aftenposten|schibsted|amedia|economist|claude\.ai|apple\.com|google \*google|google\*google|jottacloud|dropbox', 120),
    ('Sport, culture and leisure', 'ticketmaster|tikkio|letsreg|\bkino\b|filmweb|nationaltheatret|intersport|anton sport|\bxxl\b|\bg-sport', 130),
    ('Dog', 'vetzoo|zooplus|musti|dyreklinikk|dyresykehus|veterin', 140),
    ('Children', 'barnehage|\bsfo\b|leksehjelp', 150),
    ('Charity', 'flyktninghjelpen|sos-barnebyer|unicef|blindeforbund|røde kors|redd barna|kirkens', 160),
    ('Shopping, clothes and electronics', 'elkj[øo]e?p|\bpower\b|zalando|uniqlo|lindex|boozt|\blego\b|komplett|proshop|amazon|vidaxl|\bh&m\b|\barket\b|cubus|dressmann|kid interi|jysk|nille|europris|normal\b|klarna', 170)
) AS r (name, pattern, priority) JOIN category c ON c.name = r.name;

-- Transactions: the category last.
CREATE OR REPLACE VIEW report.transactions AS
SELECT t.id,
       t.account_id,
       coalesce(a.label,
                CASE WHEN a.product LIKE '%,%' THEN nullif(trim(split_part(a.product, ',', 1)), '') END,
                a.product, a.name, 'account ' || a.id) AS account,
       t.booking_date, t.value_date,
       round(t.amount_minor / 100.0, 2) AS amount,
       t.currency, t.original_amount, t.original_currency, t.exchange_rate,
       t.description, t.counterparty,
       a.is_business_income AS business_income_account,
       t.card_holder, c.kind, c.partner_txn_id AS partner_id, c.rule, t.extra,
       round(c.principal_minor / 100.0, 2) AS loan_principal,
       round(c.interest_minor / 100.0, 2) AS loan_interest,
       round(c.fees_minor / 100.0, 2) AS loan_fees,
       cat.name AS category
FROM txn t
JOIN account a ON a.id = t.account_id
LEFT JOIN txn_class c ON c.txn_id = t.id
LEFT JOIN txn_category tc ON tc.txn_id = t.id
LEFT JOIN category cat ON cat.id = tc.category_id;

-- Spending per category and month: the same spending as household_monthly
-- (card refunds and reimbursements reduce it).
CREATE VIEW report.category_monthly AS
SELECT date_trunc('month', t.booking_date)::date AS month,
       cat.name AS category,
       cat.sort_order,
       round(-sum(t.amount_minor) / 100.0, 2) AS spending,
       count(*) AS transactions
FROM txn_category tc
JOIN txn t ON t.id = tc.txn_id
JOIN category cat ON cat.id = tc.category_id
GROUP BY 1, 2, 3;

-- The rules themselves, for reading.
CREATE VIEW report.category_rules AS
SELECT r.id, c.name AS category, r.pattern, r.priority
FROM category_rule r JOIN category c ON c.id = r.category_id;
