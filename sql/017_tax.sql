-- Phase 3: the income tax estimate.
--
-- person     each taxpayer in the household, created from their pay slips
-- payslip    one row per pay slip: the year-to-date totals the estimate
--            needs and this month's regular amounts for the projection.
--            The PDF itself is in the raw store; the national identity
--            number and account number on it are not copied out.
-- tax_rule   the year's rates and limits, as adopted (data, not code)
-- business_vat  the VAT in a business deposit, where it is not the
--            standard 20% of a VAT-inclusive amount (an invoice that also
--            bills VAT afterwards for an earlier one, say)

CREATE TABLE person (
    id          serial PRIMARY KEY,
    name        text NOT NULL,                -- as on the pay slip
    name_key    text NOT NULL UNIQUE,         -- lower-case words, sorted
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE payslip (
    id                    serial PRIMARY KEY,
    person_id             int NOT NULL REFERENCES person (id),
    employer              text NOT NULL,
    employer_orgnr        text,
    period                date NOT NULL,       -- first day of the month paid for
    pay_date              date NOT NULL,
    -- year to date, after this slip
    gross_ytd_minor       bigint NOT NULL,     -- taxable salary incl. holiday pay and benefits
    tax_base_ytd_minor    bigint,              -- the employer's withholding base
    withheld_ytd_minor    bigint NOT NULL,     -- tax withheld, positive
    pension_ytd_minor     bigint NOT NULL DEFAULT 0,   -- own pension contribution, deductible
    -- this month's regular amounts, for projecting the rest of the year
    salary_minor          bigint NOT NULL,     -- fixed monthly salary
    benefits_minor        bigint NOT NULL DEFAULT 0,   -- taxable benefits in kind
    pension_minor         bigint NOT NULL DEFAULT 0,
    withholding_minor     bigint NOT NULL,     -- table withholding this month, positive
    tax_table             text,
    tax_percent           numeric(5, 2),
    net_paid_minor        bigint NOT NULL,
    lines                 jsonb NOT NULL,      -- code, text, period and year-to-date amounts
    raw_record_id         bigint REFERENCES raw_record (id),
    created_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (person_id, employer, period)
);

CREATE TABLE tax_rule (
    year        int PRIMARY KEY,
    params      jsonb NOT NULL,
    source      text NOT NULL
);

-- Income year 2026 as adopted by Stortinget (Innst. 3 S (2025-2026)): the
-- personal allowance and the top two bracket rates differ from the
-- government's proposal. Amounts in kroner.
INSERT INTO tax_rule (year, params, source) VALUES (2026, '{
  "general_rate": 0.22,
  "personal_allowance": 114540,
  "minimum_deduction_rate": 0.46,
  "minimum_deduction_max": 95700,
  "brackets": [[226100, 0.017], [318300, 0.040], [725050, 0.137], [980100, 0.168], [1467200, 0.178]],
  "social_security_salary": 0.076,
  "social_security_business": 0.108,
  "social_security_floor": 99650,
  "dividend_factor": 1.72
}', 'Innst. 3 S (2025-2026); regjeringen.no Skattesatser 2026; smartepenger.no 657');

CREATE TABLE business_vat (
    txn_id      bigint PRIMARY KEY REFERENCES txn (id),
    vat_minor   bigint NOT NULL CHECK (vat_minor >= 0),
    note        text
);
