# Skatteoppgjør (tax settlement), PDF from Skatteetaten

Layout only; no values. The app does not import these yet: a settlement is
used to check the estimate by hand (see docs/deploy.md, section 11).

Five pages, text layer (`pdftotext -layout` reads it well):

1. **Skatteoppgjør for the year.** Date of the assessment, the person's national
   identity number and the spouse's (never copy these), net wealth, general
   income before special deductions (alminnelig inntekt), personal income
   (pensjonsgivende inntekt), the result ("Til gode" or restskatt). Then the
   calculation, base and amount per line: state common tax (fellesskatt),
   municipal tax, bracket tax (trinnskatt) on personal income, social
   security (trygdeavgift) on personal income, the sum, and the settlement
   against withholding (forskuddstrekk) and advance tax.
2. **Grunnlag for skatt.** Income lines (salary and benefits, benefits in
   kind, interest, dividends with their uplift), deductions (minimum
   deduction, pension premium, loan interest, gifts, management fees), general
   income, then wealth by asset class and debt, net wealth.
3. **Grunnlag for gjeldsreduksjon.** The valuation discount on funds and
   shares and the matching reduction of deductible debt, with the spouse's
   figures.
4. **Notes** on rounding, transfers between spouses and the like.

What the page-1 and page-2 figures show about the rules:

- Personal income = salary + benefits + expense-allowance surplus; interest
  and dividends are general income only.
- General income tax is 22% of (general income - personal allowance), split
  between the state and the municipality (the split does not matter for the
  total).
- Bracket tax and social security are on personal income. Each spouse deducts
  the interest on their own loans, so the household's interest is split.
- Bases are rounded to whole kroner before the tax is computed.
