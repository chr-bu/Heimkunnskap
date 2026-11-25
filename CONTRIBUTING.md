# Contributing

Thank you for helping. Most contributions will be one of three things: support for another bank, another card's export, or another payroll system's pay slips. Each is a small, self-contained change with its own tests. The roadmap (`docs/roadmap.md`) lists larger work.

## Ground rules

These protect you and everyone whose data passes through the app.

- **No real financial data in the repository.** Not statements, exports, pay slips, receipts, database dumps or screenshots, not even redacted. Issues and pull requests count too.
- **No personal identifiers anywhere:** names, national identity numbers, account, card and loan numbers, addresses, email addresses, employee numbers.
- **Fixtures are made up.** Write a synthetic fixture with the real layout. Never edit a real file into a fixture: real files carry metadata (author, account numbers in headers, hidden sheets) that is easy to miss.
- **Tests need no network and no real bank.**
- Run `pre-commit run --all-files` before you push; it scans for secrets.

## Running the tests

```sh
pip install -r requirements.txt pytest
python -m pytest
```

The database tests are skipped unless `HEIM_TEST_PG` names an empty Postgres 17 database with the migrations in `sql/` applied (`sql/init/` creates the logins). They empty the tables first: never point them at a live database.

## Adding a bank

1. **Connect it.** If Enable Banking supports the bank, the sync works already: add it to `BANKS` in `.env`.
2. **Find its wording.** After a sync, look at your transactions (`report.transactions`) and the moves the pairing could not place (`report.review`): how the bank writes a move between your own accounts, a card bill paid, a loan instalment, a fund purchase, a dividend.
3. **Write its profile.** Copy `heimkunnskap/banks/dnb.py` to `heimkunnskap/banks/<bank>.py`, set `aspsp_names` to the bank's name exactly as Enable Banking gives it, and replace the patterns with the bank's. Leave out what the bank does not have. Add the profile to `_profiles()` in `heimkunnskap/banks/__init__.py`.
   - `card_bills` names a card brand from `CARDS` in `heimkunnskap/pairing.py`. A new card brand goes there too, with how to recognise its account.
   - If the bank's loan instalment text carries the split into principal, interest and fees, write a `loan_split` for it; otherwise leave the default (all principal).
   - `unpaired_own_transfer` is for a bank that hides an account type from PSD2 (DNB's credit-line mortgage). Most banks leave it out.
   - `unpaired_saving` names own moves that are saving when their other side is hidden from PSD2 (DNB's share savings account, ASK). It is checked before `unpaired_own_transfer`.
4. **Test it.** Add tests with made-up transactions in the bank's wording, like `tests/test_banks.py`: at least a move between own accounts, each card bill, a loan instalment (with its split, if any) and a fund purchase.
5. **Describe it.** `docs/formats/<bank>.md`: the wording, as a table like `docs/formats/dnb.md`. Wording only, never anyone's transactions.

## Adding a card export

1. Download the card's export (Excel, CSV or PDF) and look at its layout.
2. Describe the layout in `docs/formats/<card>.md`: sheets, columns, date and amount formats, how foreign currency, refunds and payments look, and any totals the file carries (the parser checks against them).
3. Write a synthetic fixture generator in `tests/` with the same layout and made-up rows, like `tests/sasfile.py`.
4. Add detection and a `parse_<card>` function to `heimkunnskap/cardfiles.py`. The card's bill text goes into the bank profiles that pay it, and its brand into `CARDS` in `pairing.py`.
5. If it earns EuroBonus points, add its earn rule with the date its terms started (`manual earn-rule`), and mention the terms in the pull request.

## Adding a pay slip layout

1. Describe the layout in `docs/formats/<payroll system>-payslip.md`: the labels for the fields the estimate needs (period, pay date, gross year to date, tax withheld year to date, salary, benefits, withholding, pension contribution, net pay).
2. Write a synthetic fixture of the text `pdftotext -layout` gives, with a made-up person, employer and amounts, like `tests/payslip_text.py`.
3. Add a parser to `heimkunnskap/payslips.py` and a test. Extract amounts only: never copy national identity numbers, addresses or employee numbers out of the slip.

## Pull requests

- One bank, card or layout per pull request.
- Say which bank, card or payroll system it is and when you checked its format; formats change.
- Explain in plain words what changes for a household that uses it.

By contributing you agree that your contribution is licensed under the project's licence (AGPL-3.0).
