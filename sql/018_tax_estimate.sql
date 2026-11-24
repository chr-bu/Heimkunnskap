-- The latest income tax estimate per year, recomputed after every sync,
-- upload and change made from the command line (tax.refresh), so the
-- report views and the Claude connector can read it without running the
-- estimate themselves.

CREATE TABLE tax_estimate (
    year        int PRIMARY KEY,
    as_of       date NOT NULL,
    result      jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE VIEW report.tax_estimate AS
SELECT e.year,
       e.as_of,
       p->>'person' AS person,
       round((p->>'salary')::numeric) AS salary,
       round((p->>'business_profit')::numeric) AS business_profit,
       round((p->>'dividends')::numeric) AS dividends,
       round((p->>'minimum_deduction')::numeric) AS minimum_deduction,
       round((p->>'pension_deduction')::numeric) AS pension_deduction,
       round((p->>'interest_deduction')::numeric) AS interest_deduction,
       round((p->>'general_income')::numeric) AS general_income,
       round((p->>'general_tax')::numeric) AS general_tax,
       round((p->>'bracket_tax')::numeric) AS bracket_tax,
       round((p->>'social_security')::numeric) AS social_security,
       round((p->>'total_tax')::numeric) AS total_tax,
       round((p->>'withheld')::numeric) AS withheld,
       round((p->>'prepaid')::numeric) AS prepaid,
       round((p->>'balance')::numeric) AS balance,
       (p->>'marginal_rate_salary')::numeric AS marginal_rate_salary,
       (p->>'marginal_rate_business')::numeric AS marginal_rate_business,
       p->'notes' AS notes
FROM tax_estimate e, jsonb_array_elements(e.result->'people') p;

CREATE VIEW report.payslips AS
SELECT s.period, s.pay_date, p.name AS person, s.employer,
       round(s.gross_ytd_minor / 100.0, 2) AS gross_ytd,
       round(s.withheld_ytd_minor / 100.0, 2) AS withheld_ytd,
       round(s.pension_ytd_minor / 100.0, 2) AS pension_ytd,
       round(s.salary_minor / 100.0, 2) AS salary,
       round(s.benefits_minor / 100.0, 2) AS benefits,
       round(s.withholding_minor / 100.0, 2) AS withholding,
       s.tax_table, s.tax_percent,
       round(s.net_paid_minor / 100.0, 2) AS net_paid
FROM payslip s JOIN person p ON p.id = s.person_id;
