# Setting up Enable Banking

Heimkunnskap reads bank accounts through [Enable Banking](https://enablebanking.com), a licensed PSD2 account information provider. Each household registers its own Enable Banking application, linked to its own accounts, and keeps the private key on its own server. These steps set that up and check what your banks give before the app is installed. They need BankID, so an account holder runs them. `scripts/eb_check.py` does the API calls.

## What Enable Banking's documentation says

Checked in November 2026. Re-check: terms change.

| Question | Finding | Status |
| --- | --- | --- |
| Is DNB supported? | Yes. DNB asks for the national identity number before redirecting to BankID. | Settled |
| Is the SAS Mastercard (SEB Kort) reachable? | The [Norway page](https://enablebanking.com/docs/markets/no/) names DNB, SpareBank 1, Nordea, Handelsbanken, Danske Bank and Sparebanken Norge. No card issuer is named. The live list from the API is the authority. | Open, step 4 |
| Does the redirect work on an internal hostname? | Not documented. The [control panel guide](https://enablebanking.com/docs/api/control-panel/) only says redirect URLs are whitelisted per application. | Open, steps 3 and 5 |
| Is restricted mode free? | The [FAQ](https://enablebanking.com/docs/faq/) describes volume pricing with a monthly minimum for contracted use, and says you can start by linking your own accounts. It states no price for that. | Open, step 6 |

New constraints found along the way:

- **Every account must be linked.** In restricted mode, an authorised account that was not linked returns an empty list.
- **Old history has a time window.** History older than 90 days may only be readable for about one hour after authorisation. The first real import must pull everything in that hour.
- **Production registration asks for more.** A description, a data protection email, a privacy policy URL and a terms of service URL.
- **The key can stay local.** The private key can be generated with OpenSSL and only the public key uploaded.
- **Unattended fetches.** About four per day per account when the user is not present. After a rate-limit error, wait six hours.

## Rules while doing this

- The private key lives outside the repository and is never pasted anywhere.
- The `code` value from the redirect is a one-time secret. Do not share it.
- The script prints counts, date ranges, opaque ids and masked account numbers only. Its output is safe to share. Raw API responses are not.

## Steps

Any machine with `python3` and `openssl` works. Nothing needs installing.

### 1. Clone the repository

```sh
git clone <repository URL> heimkunnskap
cd heimkunnskap
```

### 2. Generate a key pair for the sandbox application

```sh
mkdir -p ~/.config/heimkunnskap
chmod 700 ~/.config/heimkunnskap
openssl genrsa -out ~/.config/heimkunnskap/eb-sandbox.pem 4096
chmod 600 ~/.config/heimkunnskap/eb-sandbox.pem
openssl rsa -in ~/.config/heimkunnskap/eb-sandbox.pem -pubout -out ~/.config/heimkunnskap/eb-sandbox.pub.pem
```

If the registration form asks for a certificate instead of a bare public key, create a self-signed one from the same key:

```sh
openssl req -new -x509 -days 3650 -key ~/.config/heimkunnskap/eb-sandbox.pem \
  -out ~/.config/heimkunnskap/eb-sandbox.crt -subj "/CN=heimkunnskap"
```

### 3. Register a sandbox application

1. Sign in at https://enablebanking.com/sign-in/ with an email address. The account is created on first sign-in.
2. Open **API applications** and fill in **Add a new application**:
   - Environment: **Sandbox**
   - Key: the option to provide your own public key, then upload `eb-sandbox.pub.pem`
   - Name: `Heimkunnskap`
   - Redirect URL: `https://<internal-hostname>/eb/callback`, using the hostname the app will later have on the LAN
3. Note the application id.

If the form refuses the internal hostname, record that in the results table. It answers the redirect question early.

### 4. List the Norwegian banks and card issuers

```sh
export EB_APP_ID=<application id>
export EB_KEY_FILE=~/.config/heimkunnskap/eb-sandbox.pem
python3 scripts/eb_check.py selftest
python3 scripts/eb_check.py aspsps --country NO
```

The last lines of the output say whether any entry matches SEB Kort, Eurocard, SAS, Mastercard or American Express. A sandbox application only lists banks that offer a sandbox, so only a production application settles the SAS Mastercard question.

### 5. Test the redirect in the sandbox

No real bank data is involved. Pick `Mock ASPSP` from the list and use its name exactly as printed.

```sh
python3 scripts/eb_check.py auth --aspsp "<name from the list>" --redirect "https://<internal-hostname>/eb/callback"
```

Open the printed URL in a browser on the LAN and log in with the sandbox test user. The browser is then sent to the redirect URL. The page does not need to exist: the result is the `code` value in the address bar.

```sh
python3 scripts/eb_check.py session --code "<code>"
```

If a session id comes back, the redirect works on an internal hostname.

### 6. Register the production application

Generate a second key pair as in step 2, named `eb-prod.pem`. Then register a new application with environment **Production**. The form also asks for a description, a data protection email, a privacy policy URL and a terms of service URL.

Before continuing, check what the control panel says about cost for a restricted application.

The application starts as **Inactive**. Click **Activate by linking accounts**, choose your bank, and complete BankID. Link every account the app should read. Check that every account you expect is offered for linking under the one BankID; accounts held only by someone else need their BankID.

### 7. Read real history once

Each bank needs its own authorisation, so this runs once per bank (here DNB and the SAS EuroBonus Mastercard).

```sh
export EB_APP_ID=<production application id>
export EB_KEY_FILE=~/.config/heimkunnskap/eb-prod.pem

python3 scripts/eb_check.py auth --aspsp "DNB" --redirect "https://<your hostname>/eb/callback"
python3 scripts/eb_check.py session --code "<code>"
python3 scripts/eb_check.py history --session "<session id>"

python3 scripts/eb_check.py auth --aspsp "SAS EuroBonus Mastercard" --redirect "https://<your hostname>/eb/callback"
python3 scripts/eb_check.py session --code "<code>"
python3 scripts/eb_check.py history --session "<session id>"
```

`history --session` checks every account in the session in one run. It prints how many transactions each account returns and the earliest date, which is the real history depth.

Run `history` right after `session`: history older than 90 days may only be readable for about an hour. This consumes the window for these consents. The app's first import starts with a fresh authorisation (docs/deploy.md, Connect the banks).

## What one household found (November 2026)

Your banks may differ; add what you find for other banks to this table.

| Check | Result |
| --- | --- |
| Control panel accepts an internal (LAN-only) redirect URL | Yes, on the sandbox application; the bank sends the browser back, so the server need not be public |
| SAS EuroBonus Mastercard (SEB Kort) in the Norway list | Yes, as "SAS EuroBonus Mastercard": personal, 180 days. One card account covers both card holders. |
| American Express in the Norway list | No. Amex is a monthly file import. |
| DNB Mastercard (personal credit card) by API | No. DNB offers only current and savings accounts (CACC); the list has only DNB Corporate Mastercard. Monthly file import instead. |
| Maximum consent validity, DNB | 180 days |
| Cost for a restricted production application | None shown. The control panel says linked own accounts can be used without requesting activation. An inactive application cannot list banks (HTTP 403). |
| History depth | DNB returned five full years for every active account (the busiest about 2,700 transactions over 46 pages). The SAS EuroBonus Mastercard returned about 13 months, although five years were requested. |
