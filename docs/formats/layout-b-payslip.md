# Payroll layout B pay slip format

A private employer's payroll system, recognised by "Lønn/Trekk" and "Utbetalt HIÅ". Observed in November 2026 from a real pay slip. Only the layout is recorded here; no real values. Read by `heimkunnskap/payslips.py` (layout `layout_b`) for the tax estimate. If you know which payroll system prints this layout, a pull request naming it is welcome.

## What it is

A one-page PDF with a text layer, so `pdftotext -layout` reads it without OCR. One slip per month, paid around the 20th, in the bank feed as "Overføring Innland, <employer>" (no "Lønn" text).

## Layout

**Header:** employer name, address and organisation number; employee name and address; `Periode` (`Okt/2026`), `Betalingsdato` (`DD.MM.YYYY`), `Bet.tekst`, `Lønnskonto` (the receiving account), employee number, **national identity number**, `Skattetabell` (four digits) and `Prosenttrekk` (percent).

**Lines**, each with a code, a name, and columns for this period (`Grunnlag`, `Sats`, `Beløp`) and year to date (`Grunnlag`, `Beløp`). Deductions are negative. Grouped with a sum after each group:

| Group | Codes seen | Meaning |
| --- | --- | --- |
| Lønn | 10010 Fastlønn, 1500 Feriepenger opptjent i fjor, 0404-1 Ferietrekk | Salary, holiday pay (June), holiday deduction |
| Trekk | 7990 Skattetrekk (table), 7992 Skattetrekk prosent, 5090 Kantinetrekk | Tax withheld by table and by percent; canteen |
| Goder | 60030 Diett u/overnatting trekkfri | Tax-free allowances |
| Fordel | 2000 Livsforsikring – gruppeliv, 2050 Helseforsikring, 31201 Fordel EKT | Taxable benefits in kind: not paid out, but taxed |
| Annet | 3816 Bompenger, 60410 Kilometergodtgjørelse | Tax-free travel reimbursements |
| (unnamed) | 0404 Fastlønn trekk feriedager, 60490 Kilometergodtgjørelse trekkpliktig del | Taxable part of mileage |

**Totals:** `Skattetrekkgrunnlag`, `Skattetrekk`, `Feriegrunnlag` for the period and year to date, then `Utbetalt` and `Utbetalt HIÅ` (paid out, year to date).

## Parser rules (when built)

- Keep the period, payment date, tax table, percent, and per line the code, name and amounts. The year-to-date totals give the tax estimate its withholding so far.
- **Never keep the national identity number** or the receiving account number outside the raw store. The parser drops the header lines that hold them.
- Check: `Utbetalt` equals the bank deposit on or near `Betalingsdato`. Reimbursements can arrive separately, a few days later.
- Benefits in kind (`Fordel`) are income for tax but not cash: count them in the tax base, not in household income.
