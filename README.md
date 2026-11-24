# Heimkunnskap

A self-hosted, read-only finance app for a Norwegian household in the SAS EuroBonus ecosystem. It answers three questions:

1. Where does the household's money go?
2. Did each purchase earn the most EuroBonus points it could have?
3. How much income tax does each person owe so far this year?

Bank accounts arrive nightly through [Enable Banking](https://enablebanking.com) (PSD2, BankID consent). Card statements, the EuroBonus history, pay slips and expense claims arrive as files, uploaded or emailed. Trumf purchases and receipt lines can be fetched daily. Everything lands in one Postgres ledger with web pages, Pushover alerts, optional Grafana dashboards, and a read-only MCP server so you can ask Claude about it.

*Heimkunnskap* is Norwegian for home economics.

## What it does

- **Spending:** income and spending per month and category, with moves between your own accounts and card bill payments left out, loans split into principal and interest, savings and reimbursements kept apart.
- **EuroBonus:** expected points per purchase under each card's dated earn rule, checked against the points actually posted; points missed by paying with the wrong card; award bookings net of rebookings.
- **Groceries:** Trumf receipts down to the product line (optional; see below).
- **Tax:** a running income tax estimate per person from pay slips, loan interest and dividends, projected to year end, with VAT per term for a sole proprietorship.
- **Claude:** a read-only MCP connector over the report views you choose to share.

## Who it is for, and what works today

Built for and used by one household, so it reads what that household has: DNB accounts, an American Express SAS EuroBonus card, a DNB Mastercard, the SAS EuroBonus Mastercard, Trumf, pay slips from DFØ (state employers) and one private payroll system, and expense claims from the Unit4 travel system.

| Works for anyone | Needs a contribution for your setup |
| --- | --- |
| Enable Banking connection to any Norwegian bank it supports | A bank profile for a bank other than DNB, so its own-account moves, card bills and loans are recognised (`heimkunnskap/banks/`) |
| American Express (Norway) export | A parser for another card's export |
| SAS EuroBonus Mastercard, EuroBonus export, points engine | Your cards' earn rules (data, not code) |
| Trumf receipts | A parser for another payroll system's pay slips (entering them by hand is planned) |
| Norwegian tax rules for 2025 and 2026 | |

The roadmap is in [docs/roadmap.md](docs/roadmap.md); how to add a bank, card or pay slip layout is in [CONTRIBUTING.md](CONTRIBUTING.md).

## Each household runs its own copy

Heimkunnskap is meant to run on your own server, with your own Enable Banking application and your own BankID consents. Nobody holds anybody else's data. Hosting other people's bank data would, as far as we know, make you a provider of account information services under PSD2 (which needs a licence) and a data controller under the GDPR. Don't offer it as a service without looking into that.

## Getting started

You need a Linux server at home with Docker Compose, a hostname with public DNS (for the certificate; the app itself stays on your LAN), a reverse proxy (Apache in the guide), and an Enable Banking application of your own.

1. [docs/enable-banking.md](docs/enable-banking.md): register your Enable Banking application and check what your banks give.
2. [docs/deploy.md](docs/deploy.md): install, connect the banks, and set up card files, the tax estimate, EuroBonus, Trumf, mail intake and backups.

It is a technical setup today. A simpler one is on the roadmap.

## Caveats

- **Not advice.** The tax estimate is a planning figure; Skatteetaten's assessment is what counts. Nothing here is financial or tax advice.
- **Trumf is unofficial.** The Trumf fetcher reads the pages trumf.no shows a signed-in browser; there is no official API. It can break when the site changes, and Trumf's terms of use may not allow it. It is optional and off until you store a login.
- **Read-only.** The app never initiates payments, and stores no bank password.

## Layout

| Path | Contents |
| --- | --- |
| `heimkunnskap/` | The application: Enable Banking client, normaliser, file parsers, transfer pairing, categories, points and tax engines, Trumf fetcher, worker, web app and MCP server |
| `heimkunnskap/banks/` | Bank profiles: how each bank words own-account moves, card bills, saving and loans |
| `sql/` | Numbered migrations; `sql/init/` creates the database and its three logins |
| `docker-compose.yml`, `Dockerfile`, `.env.example` | The stack and its settings |
| `deploy/apache/` | An Apache vhost |
| `grafana/` | Grafana's data source and dashboard, provisioned from files |
| `docs/design.md` | The design |
| `docs/deploy.md` | Self-hosting guide |
| `docs/enable-banking.md` | Setting up Enable Banking, and what the banks gave |
| `docs/mcp-pattern.md` | The MCP server and how it is secured |
| `docs/formats/` | Layouts of the exports and texts the app reads |
| `docs/roadmap.md` | What comes next |
| `scripts/eb_check.py` | Checks against the Enable Banking API, standard library only |
| `tests/` | Unit tests, and database tests that run when `HEIM_TEST_PG` is set |

## Ground rules for this repository

- **No real financial data.** No statements, pay slips, exports, receipts, database dumps or screenshots, not even redacted ones.
- **Synthetic fixtures only.** Test files are made up, with the same layout as the real ones.
- **No personal identifiers.** No names, national identity numbers, account, card or loan numbers, addresses or email addresses.
- **No secrets.** Keys, tokens and `.env` files stay out. Configuration is read from the environment; `.env.example` documents the variable names.

The pre-commit hooks scan for secrets before each commit:

```sh
pipx install pre-commit      # or: sudo apt install pre-commit
pre-commit install
pre-commit run --all-files
```

## Licence

[GNU Affero General Public License v3.0](LICENSE). You may use, change and share it; if you run a changed version for other people over a network, you must offer them its source.
