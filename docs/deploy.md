# Self-hosting Heimkunnskap

Heimkunnskap runs as one Docker Compose stack on a Linux server at home, here in `/srv/heimkunnskap`, behind Apache. Commands are run on the server unless a step says otherwise. Each household runs its own copy: see the README for why.

Before you start: an Enable Banking application of your own (docs/enable-banking.md), a hostname for the server with public DNS (for the certificate; the app itself stays on your LAN), Docker with Compose, and Apache with certbot.

What runs:

| Container | Does | Reachable |
| --- | --- | --- |
| `postgres` | The ledger. Three logins: owner (worker and web app), `_ro` (Grafana), `_mcp` (Claude connector) | Docker network only |
| `worker` | Applies migrations and grants on start, then syncs every bank at `SYNC_AT` each night | Nowhere |
| `web` | Status page, BankID consents, account labels, and `/mcp` | `127.0.0.1:4200`, through Apache |
| `grafana` | Dashboards, under the `grafana` profile until the first dashboard exists | `127.0.0.1:3001` |

## 1. Get the code

```sh
sudo install -d -o "$USER" -g "$USER" /srv/heimkunnskap
git clone <repository URL> /srv/heimkunnskap
cd /srv/heimkunnskap
```

Git commands in this directory run as you, without `sudo`. Docker commands and the root-only files (`.env`, `secrets/`) use `sudo`.

## 2. Secrets

Two root-only files, outside the repository and outside Docker volumes:

```sh
sudo install -d -m 700 -o root -g root /srv/heimkunnskap/secrets
```

**The Enable Banking key.** The private key of your production application (docs/enable-banking.md, step 6). Copy it over SSH only, never through cloud storage or chat. From the machine that has it:

```sh
scp ~/.config/heimkunnskap/eb-prod.pem <server>:eb-key.pem
```

Then on the server:

```sh
sudo install -m 600 -o root -g root ~/eb-key.pem /srv/heimkunnskap/secrets/eb-key.pem
shred -u ~/eb-key.pem
```

**Pushover**, for alerts to the phone. In the Pushover dashboard (pushover.net), create an application named Heimkunnskap (the coin icon in `heimkunnskap/web/static/apple-touch-icon.png` works as its icon) and note its API token; your user key is on the dashboard's front page. Then, two lines:

```sh
sudo sh -c 'umask 077; cat > /srv/heimkunnskap/secrets/pushover'
token=<application API token>
user=<your user key>
```

Type or paste the two lines after the command, press Enter, then Ctrl-D. The labels may be left out: the token alone on the first line and the user key on the second works too. To check it without printing it, `sudo wc -l /srv/heimkunnskap/secrets/pushover` should say 2. Without the file Docker would create a directory in its place, so it must exist before the stack starts.

Alerts that need doing come as normal Pushover messages, with a link to the page to act on; confirmations (a file imported by mail) come without sound. None break through quiet hours: the nightly sync runs at 02:30.

**The Gmail app password** for the mail intake (section 12). One line; an empty file while mail intake is off, since the worker mounts it either way:

```sh
sudo sh -c 'umask 077; cat > /srv/heimkunnskap/secrets/gmail-app-password'
```

Both files must exist before the first start. If a bind-mounted file is missing, Docker creates an empty directory in its place.

## 3. Settings

```sh
sudo cp .env.example .env
sudo chmod 600 .env
sudo nano .env    # sudoedit refuses: the directory is yours, not root's
```

Fill in every empty value. Generate each password, `WEB_SECRET_KEY` and `MCP_TOKEN` with:

```sh
openssl rand -base64 32 | tr -d '/+=' | cut -c1-43
```

- `EB_APP_ID`: your production application id (docs/enable-banking.md).
- `BANKS`: the banks to connect, as Enable Banking names them.
- `BUSINESS_INCOME_ACCOUNTS`: only with a sole proprietorship: its account number. Leave empty otherwise.
- `WEB_ALLOWED_NETWORKS`: the home LAN, the WireGuard range and the home's public address as `/32`. Use the same addresses as the Apache rule in step 5.
- `MCP_TOKEN`: leave empty for now. Step 8 turns the connector on.

## 4. Start

```sh
sudo docker compose build
sudo docker compose up -d
sudo docker compose logs worker
```

The worker log should show both migrations applied, then:

```
read-only grants ensured for heimkunnskap_ro
MCP login may read: accounts, monthly, balances, consents, imports
worker started; nightly sync at 03:15 Europe/Oslo
```

Check the web app locally:

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4200/healthz
```

It should print `200`.

## 5. Apache

The vhost is `deploy/apache/heimkunnskap.conf`. Install it as `/etc/apache2/sites-available/heimkunnskap.conf`:

```sh
sudo cp deploy/apache/heimkunnskap.conf /etc/apache2/sites-available/heimkunnskap.conf
sudoedit /etc/apache2/sites-available/heimkunnskap.conf
```

Replace `heim.example.org` with your hostname, and the example addresses in `Require ip` under `<Location />` with your LAN range, your VPN range (WireGuard, say) and your home's public address. If the hostname has an AAAA record, add the home IPv6 prefix too, or LAN clients on IPv6 are refused.

Check the Anthropic range for `/mcp` against https://platform.claude.com/docs/en/api/ip-addresses.

Get the certificate first, since the vhost refers to it. The hostname needs public DNS for the HTTP challenge:

```sh
sudo certbot certonly --apache -d heim.example.org
```

Then enable the site:

```sh
sudo a2enmod ssl proxy proxy_http headers
sudo a2ensite heimkunnskap
sudo apachectl configtest && sudo systemctl reload apache2
```

If no catch-all vhost on port 80 redirects to HTTPS, run `sudo certbot --apache -d heim.example.org` once more and let it add the redirect.

**Check the rules.** From a laptop on the LAN, `https://heim.example.org/` shows the overview. From a phone on mobile data with WireGuard off, it answers 403. `https://heim.example.org/mcp` answers 403 from both, because neither is in Anthropic's range.

## 6. Connect the banks

Consents from testing (docs/enable-banking.md) are not used: their one-hour window for old history is spent, so each bank needs a fresh BankID now.

On the LAN or over WireGuard, open `https://heim.example.org/`:

1. Click **Connect <bank> with BankID** (one button per bank in `BANKS`) and complete BankID. Select every account.
2. Back on the overview, the Imports table fills in as the full history arrives. A busy account can take dozens of pages, so allow a few minutes.
3. Do the same for each bank, such as **SAS EuroBonus Mastercard**.

Type whose BankID it is in the field next to the button ("Ola", "Kari"). The name shows on the overview and in the expiry alerts, and the alert's renewal link fills it in again.

**Accounts only someone else reaches.** A consent covers the accounts the person signing in holds or may use. Accounts only your partner holds need their BankID: they connect the bank with their name in the field. Their consent sits beside yours. A renewal replaces only the consent it fully covers, so renewing yours leaves theirs in place. An account under both consents is synced once a night, under the consent that lasts longer.

**Is your bank's wording known?** Moves between your own accounts, card bills and loan payments are recognised from each bank's text, through a bank profile (`heimkunnskap/banks/`). DNB has one. For a bank without one, the sync works, but those payments count as income and spending until a profile is added: see CONTRIBUTING.md.

Check the result:

- Every account appears under Accounts.
- With a sole proprietorship, one account carries the **business income** mark.
- Every import row ends with **done**.
- Each consent shows about 180 days left.

The sync log says how many transactions came with a bank reference:

```sh
sudo docker compose logs web | grep 'bank reference'
```

That tells which banks give stable references. The fingerprint check covers banks that don't.

## 7. Nightly sync

The worker syncs every active consent at `SYNC_AT` (03:15 Oslo time by default), before a typical nightly backup. Keep `SYNC_AT` clear of the server's own maintenance: if unattended-upgrades may reboot it (`Unattended-Upgrade::Automatic-Reboot-Time` in `/etc/apt/apt.conf.d/50unattended-upgrades`), pick a time well after. A sync that loses the database is tried again twice, five minutes apart, and a worker that starts after `SYNC_AT` without that night's sync finished runs it at once. Each night it reads transactions from ten days before the newest stored booking, and one balance per account. That is two requests per account, within the bank's limit of about four unattended requests a day.

Alerts go to Pushover:

- a consent ends within 14 days, repeated every three days;
- a consent has expired or been revoked;
- a sync failed.

Each alert carries the link that renews that bank's consent.

To sync right away:

```sh
sudo docker compose exec worker python -m heimkunnskap.sync --once
```

## 8. Claude connector

Set `MCP_TOKEN` in `.env`, then restart the web app:

```sh
sudo docker compose up -d web
```

In Claude, add a custom connector with the URL `https://heim.example.org/mcp` and the token as a request header: `Authorization: Bearer <token>`. The bare token works too.

By default Claude sees totals only: monthly money in and out per account, balances, consents and imports. To share individual transactions and merchants, add `transactions` to `MCP_VIEWS` and restart the worker, which re-applies the grants:

```sh
sudo docker compose up -d worker
```

Every tool call is logged in `docker compose logs web` with its arguments. A refused token is logged with the SHA-256 fingerprints of the token sent and expected. To compare:

```sh
sudo grep '^MCP_TOKEN=' .env | cut -d= -f2- | tr -d '\n' | sha256sum
```

## 9. Backups

Add three things to your nightly backup, inside the part that encrypts it (GPG, say) before it leaves the server:

```sh
# Heimkunnskap: the ledger, its settings and its key
docker compose -f /srv/heimkunnskap/docker-compose.yml exec -T postgres \
    pg_dump -U admin -Fc heimkunnskap < /dev/null > "$WORK/heimkunnskap.pgdump"
cp /srv/heimkunnskap/.env "$WORK/heimkunnskap.env"
tar -C /srv/heimkunnskap -cf "$WORK/heimkunnskap-secrets.tar" secrets
```

`$WORK` stands for the backup's staging directory. The dump holds the raw store and every transaction, and the key can read the accounts while a consent is active. None of the three may leave the server unencrypted.

**Test a restore once.** Load the dump into a scratch container and count the rows:

```sh
sudo docker run --rm -d --name heim-restore -e POSTGRES_PASSWORD=x postgres:17
sudo docker cp "$WORK/heimkunnskap.pgdump" heim-restore:/tmp/
sudo docker exec heim-restore sh -c \
    'sleep 5; createdb -U postgres heimkunnskap && pg_restore -U postgres -d heimkunnskap --no-owner /tmp/heimkunnskap.pgdump; psql -U postgres -d heimkunnskap -c "select count(*) from txn"'
sudo docker rm -f heim-restore
```

Run this on a decrypted copy, then delete the copy.

## 10. Card files and dashboards (phase 2)

**Card files.** Once a month, after each card's statement, download the Excel export:

- **Amex:** americanexpress.com, account activity for the closed billing period, as Excel.
- **DNB Mastercard:** the card's transaction list in the DNB online bank, as Excel.
- **SAS EuroBonus Mastercard**, only for history older than the bank feed's (about 13 months): the transaction export at SEB Kort, as Excel. The file is merged into the card's bank-feed account and adds only rows older than the feed's first transaction, so uploading an overlapping period is harmless. The upload result says how many of the overlapping rows it found in the feed.

Upload them at `https://heim.example.org/upload`. Several files can go at once, in any order and with any name. A file uploaded twice is skipped, and a period that has grown since the last upload adds only its new rows. If a month passes without a new file for a card, a reminder comes by Pushover.

**Transfer pairing** runs after every nightly sync and every upload. It leaves moves between your own accounts and card bill payments out of income and spending. A card bill for a period with no card data (no file, or older than the bank feed) counts as spending instead, under *Card purchases (no detail)*, until a file for that period is uploaded. The Claude connector and Grafana show anything it could not pair under `review`.

**Spending categories** are on `https://heim.example.org/categories`: totals per category for the year, the largest payees still in *Other* with a one-click rule, and every rule to edit. A rule is a regular expression matched against the payee and bank text in lower case; the first match by priority wins. Every change re-sorts the whole history at once, as does every sync and upload.

**What the bank text cannot tell.** Some payments look like spending but are not: a loan to a company of your own, or shares bought in it. Mark them from the command line, in `/srv/heimkunnskap`. First find the transaction and its id:

```sh
sudo docker compose exec worker python -m heimkunnskap.manual find "holding as"
```

A loan you made: give its id and a pattern that matches the repayments' text (case does not matter). The repayments are found by that pattern, now and in every later sync. They pay off the loan first; what comes back beyond the amount lent is interest, which counts as income. Once the loan is settled, add `--until` with the day of the last payment, so interest paid after the principal is counted with the loan too:

```sh
sudo docker compose exec worker python -m heimkunnskap.manual lend 1234 --repaid-by "holding as" --until 2025-05-05 --note "loan to the company"
```

Running `lend` again for the same id changes the pattern, date or note.

An investment or other saving, a loan payment, or a reimbursement the rules miss:

```sh
sudo docker compose exec worker python -m heimkunnskap.manual mark 1233 saving
```

**A payment to the rammelån that the bank split.** The rules take a direct payment to the credit line for all principal. When the month's interest was taken out of the payment instead of being debited around the 20th, the loan's statement in the DNB app (Siste transaksjoner) shows the split. Give it per payment; the three amounts must add up to the payment:

```sh
sudo docker compose exec worker python -m heimkunnskap.manual check
sudo docker compose exec worker python -m heimkunnskap.manual split 1452 --principal 29598.65 --interest 10401.35
sudo docker compose exec worker python -m heimkunnskap.manual split 1451 --interest 14935 --fees 65 --principal 0
```

`check` lists the months with a direct payment to the rammelån and no interest debit for it, which is how to find them; `list` mentions them at the end.

`list` shows every loan and mark, `unmark ID` undoes one (or a split). The names stay in the database, not in the code.

**Grafana** runs at `https://heim.example.org/grafana/`, behind the same Apache rule as the web app. To enable it, add the Grafana lines to the vhost. Re-copying the repository's version and putting your addresses back is the simplest way:

```sh
sudo cp /srv/heimkunnskap/deploy/apache/heimkunnskap.conf /etc/apache2/sites-available/heimkunnskap.conf
sudoedit /etc/apache2/sites-available/heimkunnskap.conf     # your hostname and addresses again
sudo a2enmod proxy_wstunnel
sudo apachectl configtest && sudo systemctl reload apache2
sudo docker compose --profile grafana up -d
```

Sign in as `admin`, with the password from `.env`:

```sh
sudo grep '^GRAFANA_ADMIN_PASSWORD=' .env | cut -d= -f2-
```

The dashboard is under Dashboards, Heimkunnskap, *Household spending*. It is provisioned from `grafana/provisioning/dashboards/json/spending.json`, so changes made in Grafana are not kept: change the file instead.

## 11. Tax estimate (phase 3)

**Pay slips.** Upload the newest pay slip for each person at `https://heim.example.org/upload`, the same place as the card files. Two layouts are read so far: DFØ's (Norwegian state employers) and one private payroll system's (layout B, `docs/formats/layout-b-payslip.md`). A pay slip from another payroll system needs a parser (CONTRIBUTING.md); entering the figures by hand is planned (docs/roadmap.md). One recent slip per person is enough; the estimate takes the year-to-date figures from it and projects the rest of the year at that month's salary, with half tax in December. A newer slip replaces the projection. The upload result says whether the net pay matches a deposit in the bank, and whether tax withheld carries on from last month's slip.

**A password-protected slip** is refused with a message saying so. Some payroll systems lock the PDF with a password (often the birth date or national identity number). Heimkunnskap does not keep that password, so save an unprotected copy first and upload that. On a Mac, with qpdf (`brew install qpdf`):

```sh
read -rs PW && qpdf --decrypt --password="$PW" "PayrollSlip.pdf" "PayrollSlip-open.pdf"; unset PW
```

Type the password (it is not shown) and press Enter. Or open the slip in Preview, give the password, and choose File → Export as PDF… with Encrypt unticked.

**Settings,** in `/srv/heimkunnskap`:

```sh
# The date the business was registered for VAT: deposits from then on hold 20% VAT
sudo docker compose exec worker python -m heimkunnskap.manual set vat_registered_from 2026-04-19
# A deposit whose VAT is not 20% of it: give its id (from manual find) and the VAT in kroner
sudo docker compose exec worker python -m heimkunnskap.manual vat 1453 15000
# Who deducts the shared loan interest (default: the business owner)
sudo docker compose exec worker python -m heimkunnskap.manual set loan_interest_person "Ola Eksempel Nordmann"
# That person's share (0-1) of the household's loan interest: each spouse deducts the interest
# on their own loans, and the rest goes to the other person. Last year's settlements show the split.
sudo docker compose exec worker python -m heimkunnskap.manual set loan_interest_share 0.5
```

**The estimate** is at `https://heim.example.org/tax`, per person, with what it assumes. From the command line: `manual tax`. Claude's connector has it as the `tax_estimate` tool. It is recomputed after every sync, upload and setting change.

Not covered yet: wealth tax, student loan interest, credit interest, gains on funds and shares sold, gifts to voluntary organisations and management fees (small deductions, the settlement lists them), and business expenses.

**Checked against a real result:** the 2025 rules (`sql/019_tax_2025.sql`) reproduced a real skatteoppgjør 2025 to the krone: trinnskatt and trygdeavgift exactly, general income tax within a krone once the gifts and fees on the settlement are included.

## 12. Expense claims and mail intake

**Expense claims** (reiseregning) from the employer's travel system are PDFs like pay slips: upload them at `/upload`, or email them (below). Each claim is checked against its own totals, so a misread one is refused rather than stored wrong. Then:

- each outlay (hotel, flight, taxi, train, parking, tolls) is matched to the card payment it was. A hotel bill larger than the claim is fine: the extras not claimed stay as your own spending;
- the claim is matched to the salary deposit that paid it: the total less tax withheld at the pay slip's percentage on the taxable part;
- that deposit is split. What it pays back reduces spending in the card payment's category (Travel, Car and transport), in the deposit's month; the rest (per diem, mileage and other allowances, less tax) is income.

Claims that need a decision:

```sh
sudo docker compose exec worker python -m heimkunnskap.manual claims
# outlay line 812 was card payment 4819, not the one guessed:
sudo docker compose exec worker python -m heimkunnskap.manual claim-line 812 4819
# not paid by card (cash, a monthly toll bill): still paid back, counted as Travel or Car and transport
sudo docker compose exec worker python -m heimkunnskap.manual claim-line 815 none
# a deposit that paid two claims at once:
sudo docker compose exec worker python -m heimkunnskap.manual claim-payout 100001 2130
```

`auto` in place of a transaction id undoes a choice. The person's national identity number, address, employee number and the route lines of mileage claims are not copied out of the PDF.

**Mail intake.** The worker reads a dedicated Gmail mailbox over IMAP every five minutes and imports the PDF and Excel attachments of mail from the addresses in `MAIL_ALLOWED_SENDERS`, the same way as an upload: card files, pay slips and expense claims.

A message is used only if its From address is on the list *and* Gmail's own sender checks (DMARC, or DKIM or SPF for that domain) passed, read from the Authentication-Results header Gmail adds on receipt. Mail from anyone else is labelled *Heimkunnskap/Rejected* and ignored; a trusted address that fails the checks also gets an alert. Imported mail is labelled *Heimkunnskap/Imported*, and each import is reported with a quiet Pushover message. Only `.pdf` and `.xlsx` attachments whose contents really are PDF or Excel, up to 15 MB, are read.

Setting it up:

1. In the Google account of the intake address: turn on 2-Step Verification (Security), then create an app password (Security, App passwords, name it Heimkunnskap). IMAP is on by default.
2. Put the app password in the secrets file (section 2). Google shows it in four groups; the spaces do not matter.
3. In `.env`:

   ```sh
   MAIL_USER=<the intake address>@gmail.com
   MAIL_ALLOWED_SENDERS=<work address>,<private address>
   ```

4. Rebuild and restart (the image now has `pdftotext` and `tesseract` for the claims), then watch the worker's log:

   ```sh
   sudo docker compose build && sudo docker compose up -d
   sudo docker compose logs worker --tail 20 | grep -i mail
   ```

   `mail intake: <address> every 300 s` means it is polling. A failure to log in comes as an alert once, and again when it works.

Changing the app password: write the new one to the secrets file and `sudo docker compose restart worker`.

## 13. Business expenses and VAT

The sole proprietorship's expenses are household payments tagged as the business's, on the **Business** page (`/business`: find the payment, give the business's share and the VAT rate, Tag) or from the command line. Your tag decides; no receipt is asked for. A tagged expense:

- lowers the business profit in the tax estimate by its cost excl. VAT;
- carries input VAT at its rate (25% unless you pick 15, 12 or 0, or give the amount), from the VAT registration date; before it, the VAT is part of the cost;
- moves to the category *Business* when it is wholly the business's.

```sh
sudo docker compose exec worker python -m heimkunnskap.manual find adobe
sudo docker compose exec worker python -m heimkunnskap.manual tag 5123                      # all of it, 25% VAT
sudo docker compose exec worker python -m heimkunnskap.manual tag 5124 --share 0.5          # half: a phone bill
sudo docker compose exec worker python -m heimkunnskap.manual tag 5125 --vat 508 --note "office chair"
sudo docker compose exec worker python -m heimkunnskap.manual business-rule adobe --note software
sudo docker compose exec worker python -m heimkunnskap.manual business
```

A rule tags every past and future payment it matches; a tag by hand wins over a rule. `untag ID` and `business-rule-delete RULE` undo.

**VAT per term.** `manual business` and the Business page show each two-month term since registration: output VAT (20% of each deposit into the business account unless set with `manual vat`), input VAT, what was paid to Innkrevingsmyndigheten or refunded, and what is outstanding. A payment counts against the term that ended before it; a refund names its term. A deposit whose VAT belongs to an earlier term (an invoice paid late) is moved with `--term`:

```sh
sudo docker compose exec worker python -m heimkunnskap.manual vat 1453 15000 --term 2026-04 --note "invoice 3-26, incl. March"
```

Reminders come by Pushover at 09:00: when a term with VAT to pay has ended, three days before it is due, and the day after if no payment is seen. Terms more than 30 days past due are not chased.

## 14. EuroBonus points

On sas.no, open My EuroBonus, Transactions, and export to Excel. Upload the file at `/upload`, or email it to the intake address. Each export repeats the whole history; only new rows are added, so export as often as you like (once a month after DNB's posting around the 23rd is enough).

The **Points** page (`/points`) and `manual points` show:

- points earned and level points by source, this year and the last 12 months, and award travel kept. A changed or cancelled award booking comes back as a refund from SAS of exactly its points; each refund is paired with its booking and both are left out, so neither earned points nor award spending is inflated by rebookings;
- per card and posting month: point-earning spend, points expected under the card's earn rule, points received, and the difference. A card can post a month late, so look at the year's total before chasing a month;
- points missed: what the best card would have earned more, per merchant. American Express counts only at merchants where it has been used;
- what earns nothing: Vipps payments to people, crowdfunding, card fees, and spending over a card's yearly cap.

Earn rules are dated. When a card's terms change:

```sh
sudo docker compose exec worker python -m heimkunnskap.manual earn-rule dnb --from 2027-01-01 --nok 15 --foreign 20 --cap 700000
sudo docker compose exec worker python -m heimkunnskap.manual points-exclude 'vipps\*example shop' --note "earns nothing"
```

The rules seeded by `sql/022_points.sql` are one household's cards in November 2026: American Express SAS EuroBonus 20 points per 100 kr, at home and abroad; DNB Mastercard with the SAGA Upgrade 15, and 20 in foreign currency, up to 700,000 kr of spending a calendar year; SAS EuroBonus Mastercard 15. Set your own cards' current terms with `earn-rule`. For that household, DNB's postings matched the rule within about 1% most months.

## 15. Trumf purchases and receipts

Optional, and unofficial. The worker reads trumf.no every morning at 07:10 with the household's own login: each purchase in NorgesGruppen stores and through Trumf Netthandel, and each receipt line (product, barcode, quantity, price, Trumf bonus and why). Trumf keeps 12 months; Heimkunnskap keeps everything from the first fetch on. There is no official API: if trumf.no changes its pages, the fetch stops and Pushover says so once.

Store the login once. The cookie is a login to the Trumf account, so treat it like a password: don't paste it anywhere but the command below, and don't save it in a file.

1. In a desktop browser, log in at https://www.trumf.no and open **Min side → Kvitteringer**.
2. Open the developer tools (F12 or Cmd-Option-I), the **Network** tab, and reload the page.
3. Click the request named `kvitteringer` (or any request to `www.trumf.no`). Under **Request Headers**, find `Cookie`, right-click its value and choose **Copy value**. It is long, and it holds `__Secure-authjs.session-token` (perhaps as `.0` and `.1`).
4. On the server:

```sh
cd /srv/heimkunnskap
sudo docker compose exec worker python -m heimkunnskap.manual trumf-session
```

Paste at the prompt and press Enter. Nothing is shown while you paste, and the line may be longer than a terminal line usually allows; that is handled. Only the session cookie is kept; the rest of the header is dropped.

5. The first fetch reads the year's receipts, about a second each (some 400 receipts: about ten minutes):

```sh
sudo docker compose exec worker python -m heimkunnskap.manual trumf --fetch
```

After that, `manual trumf` shows purchases per month and chain and the login's state, and the **Groceries** page (`/groceries`) shows the same with the products the money goes to.

The login lasts a year from its last use, and the daily fetch uses it, so it should hold for as long as it is used. Logging out in the browser you copied it from may end it as well (Trumf may revoke the session at id.trumf.no): close that tab rather than logging out. When it ends, Pushover says "Trumf login has ended"; repeat steps 1-4.

## Updating

```sh
cd /srv/heimkunnskap
git pull
sudo docker compose build
sudo docker compose up -d
```

New migrations are applied when the worker starts.

## Running the tests

On any machine with Python 3.12 and the requirements installed:

```sh
pip install -r requirements.txt pytest
python -m pytest
```

The database tests are skipped unless `HEIM_TEST_PG` names an empty test database with the schema applied. They empty the tables first. Never point them at the live database.
