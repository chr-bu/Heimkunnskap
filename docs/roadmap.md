# Roadmap

Heimkunnskap was built for one household. These steps make it something other households can run. Contributions to any of them are welcome; see CONTRIBUTING.md.

## Done

- **Bank profiles.** How each bank words moves between own accounts, card bills, saving, dividends and loan payments is one profile per bank (`heimkunnskap/banks/`), chosen by the account's bank name in Enable Banking. DNB's is `banks/dnb.py`, described in `docs/formats/dnb.md`; `tests/test_banks.py` runs a made-up second bank. A bank without a profile still syncs, but its own-account moves, card bills and loans count as income and spending until it has one.

## Next

### Pay slips by hand

A form on the Tax page (and `manual payslip`) for one month's figures: period, employer, gross pay year to date, tax withheld year to date, this month's salary, benefits and withholding, pension contribution. These are the fields the estimate uses (`project_slip()` in `tax.py`), and every Norwegian pay slip shows them.

- Done when a household with no parser for its pay slips gets the same estimate from typed figures.
- Parsers then become a convenience: `payslips.py` gets a list of layouts, each with `detect(text)` and `parse(text)`, so a new payroll system (Visma, Huldt & Lillevik, SAP, Tripletex) is one function and one fixture.

### Optional modules

Settings that switch off what most households do not need, off by default:

- `MODULE_BUSINESS`: sole proprietorship, business expenses, VAT terms and reminders
- `MODULE_CLAIMS`: expense claims, with the Unit4 layout as the first one
- `MODULE_TRUMF`: the Trumf fetcher

Switched off, a module's page leaves the menu, its worker loop does not start, and the tax estimate leaves its part out.

### Settings instead of assumptions

- People: the `person` table is filled from pay slips today. Make it a setup step: names, and which BankID label is whose.
- Document the VAT registration date, the loan interest split and the business account as optional settings.
- A category rule set that each household starts from and edits, rather than one seeded list.
- Card brands (`CARDS` in `pairing.py`) and the cards' earn rules as setup data, not migrations.

### Simpler setup

The current setup assumes a Linux server with Docker Compose, Apache in front, root-only secret files and an existing backup routine. For others:

- a Compose file that runs without Apache, on localhost, with a reverse proxy as an option (Caddy is the shortest to document)
- a setup guide from nothing: Enable Banking application and key, first consent, first upload, backups
- safe defaults: the MCP endpoint off until a token is set (already so), the web app bound to localhost, no default passwords
- a backup and restore command in the app (`manual backup`)

### More banks, cards and payroll systems

Each is a contribution of its own: a bank profile (Nordea, SpareBank 1, Handelsbanken, Danske Bank, Sbanken...), a card export parser (Bank Norwegian, re:member, Trumf Visa...), a pay slip layout.

### Product categories for grocery receipts

Trumf receipt lines carry product text and usually the EAN. Group them (dairy, meat, fruit and vegetables, drinks, household goods) so the Groceries page can show where the money goes by kind, not just by product.

## Open questions

- **Wealth tax and share income** in the tax estimate.
- **Trumf.** The fetcher reads pages meant for a browser. Whether Trumf's terms allow it, and whether NorgesGruppen would offer an export, is not settled.
