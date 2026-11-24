# DFØ pay slip format (Norwegian state employers)

Observed in November 2026 from a real pay slip. Only the layout is recorded here; no real values. Read by `heimkunnskap/payslips.py` (layout `dfo`) for the tax estimate.

## What it is

A one-page PDF with a text layer (small, about 6 KB). One slip per month, paid around the 12th, in the bank feed as "Lønn, <employer>". Smaller extra deposits with the same text (expenses, back pay) come on other days.

## Layout

**Header:** `Firmanavn/ Avsender` with the employer's address, `Lønnsmottagers navn` and address, `Arbeidsgivernr.` (organisation number), `Periodenr` (`YYYYMM`), `Dato` (`YYYYMMDD`, the payment date), **`Fødselsnr.`**, `Ansatt nr.`, `Bankkontonr.` (11 digits, no dots).

**Position:** one row with `St.kode`, `Betegnelse` (title), `St-%` (position percentage), `LR`, `Ansiennitet` (date) and `Ltr./Sats` (yearly salary in kroner).

**Lønnsspesifikasjon:** rows of code, text, `Tab / %` (the tax table on the table-withholding row, the percent on the percent row), then this period's and year-to-date amounts. Deductions are negative. Codes seen:

| Code | Text | Meaning |
| --- | --- | --- |
| 1100 | Regulativlønn | Monthly salary |
| 1100S | ER: Regulativlønn | Back pay after a pay settlement (`S` suffix: correction) |
| 1150 | Trekk OU-fond | Union fund deduction |
| 1163 | Gruppelivsforsikring | Group life: a taxable benefit, not paid out |
| 1850S | Etterbetaling (1850): Trekk ferie | Holiday deduction correction |
| 3121 | Fordelsbeskatning elektroniske tjenester | Phone and broadband benefit: taxable, not paid out |
| 7200 | Pensjonstrekk SPK 2% | Pension contribution, deductible |
| 8910 | Forskuddstrekk | Tax withheld by table |
| 8920 | Forskuddstrekk % | Tax withheld by percent |

**Year-to-date totals** below the rows: `Feriepengegrunnlag`, `Skattetrekkgrunnlag`, `Bruttolønn`, `Forskuddstrekk`. At the bottom: `Netto utbetalt` and `Utbetalt til` (account).

## Parser rules (when built)

- Keep the period, payment date, yearly salary, tax table and percent, and per code the period and year-to-date amounts.
- **Never keep the national identity number** or the account number outside the raw store.
- Check: `Netto utbetalt` equals the "Lønn, <employer>" deposit on `Dato` or the next banking day.
- The pension contribution (7200) and union deduction (1150) reduce the tax base; benefits (1163, 3121) add to it without being cash.
