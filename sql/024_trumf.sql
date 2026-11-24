-- Trumf purchases and their receipt lines, fetched from trumf.no with the
-- household's stored login session (heimkunnskap/trumf.py).

-- The trumf.no login session: the Auth.js session cookie (one or more
-- pieces, __Secure-authjs.session-token[.N]). Trumf renews it on use, so
-- the worker writes back what it is sent. Not a report view: the Claude
-- connector cannot read it.
CREATE TABLE trumf_session (
    id           int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    cookies      jsonb NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    last_ok_at   timestamptz,
    last_error   text
);

CREATE TABLE trumf_txn (
    batch_id            text PRIMARY KEY,           -- Trumf's id for the purchase
    member_id           text NOT NULL DEFAULT '',
    at                  timestamptz NOT NULL,       -- transaksjonsTidspunkt
    category            text NOT NULL,              -- ACCRUAL (a purchase), CONSUME (bonus used)
    partner             text NOT NULL DEFAULT '',   -- KIWI, MENY, SPAR, JOK, VIAT (web shop), EB, ...
    store               text NOT NULL DEFAULT '',   -- beskrivelse
    amount_minor        bigint NOT NULL,            -- belop
    bonus_minor         bigint NOT NULL,            -- Trumf bonus, øre; negative when used
    savings_minor       bigint NOT NULL DEFAULT 0,  -- totaleBesparelser
    has_receipt         boolean NOT NULL,
    receipt_fetched_at  timestamptz,
    txn_id              bigint REFERENCES txn (id) ON DELETE SET NULL,   -- the card payment
    raw                 jsonb NOT NULL,
    first_seen_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX trumf_txn_at ON trumf_txn (at);

CREATE TABLE trumf_line (
    batch_id       text NOT NULL REFERENCES trumf_txn (batch_id) ON DELETE CASCADE,
    line_no        int NOT NULL,
    product        text NOT NULL,              -- produktBeskrivelse
    ean            text,
    quantity       numeric(12, 3),
    unit           text,                       -- enhetsType: EA (each), KG, ...
    amount_minor   bigint NOT NULL,
    bonus_minor    bigint NOT NULL DEFAULT 0,
    bonus_pct      numeric(6, 2),
    vat_pct        numeric(6, 2),
    unknown_item   boolean NOT NULL DEFAULT false,
    rules          jsonb NOT NULL DEFAULT '[]',   -- bonusregler: which rule gave the bonus
    savings        jsonb NOT NULL DEFAULT '[]',   -- besparelser: discounts
    PRIMARY KEY (batch_id, line_no)
);

CREATE VIEW report.trumf_purchases AS
SELECT p.batch_id, (p.at AT TIME ZONE 'Europe/Oslo')::date AS day, p.at, p.category, p.partner,
       p.store, round(p.amount_minor / 100.0, 2) AS amount, round(p.bonus_minor / 100.0, 2) AS bonus,
       round(p.savings_minor / 100.0, 2) AS savings, p.has_receipt,
       (SELECT count(*) FROM trumf_line l WHERE l.batch_id = p.batch_id) AS lines,
       p.txn_id, coalesce(a.label, a.name, a.product, 'account ' || a.id) AS paid_with
FROM trumf_txn p
LEFT JOIN txn t ON t.id = p.txn_id
LEFT JOIN account a ON a.id = t.account_id;

CREATE VIEW report.trumf_lines AS
SELECT l.batch_id, (p.at AT TIME ZONE 'Europe/Oslo')::date AS day, p.partner, p.store, l.line_no,
       l.product, l.ean, l.quantity, l.unit, round(l.amount_minor / 100.0, 2) AS amount,
       round(l.bonus_minor / 100.0, 2) AS bonus, l.bonus_pct, l.vat_pct, l.unknown_item,
       (SELECT string_agg(r ->> 'bonusregelBeskrivelse', '; ') FROM jsonb_array_elements(l.rules) r)
           AS bonus_rules
FROM trumf_line l JOIN trumf_txn p ON p.batch_id = l.batch_id;
