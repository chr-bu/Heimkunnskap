#!/usr/bin/env python3
"""Checks against the Enable Banking API, before installing the app.

Uses only the Python standard library plus the `openssl` command, so there is
nothing to install. It never prints transaction rows, account numbers in full,
or the signed token: only counts, date ranges, opaque ids and masked numbers.

Configuration comes from the environment:

    EB_APP_ID     the application id shown in the Enable Banking control panel
    EB_KEY_FILE   path to the application's RSA private key (PEM)

Commands:

    selftest                      build a token locally and check the key (no network)
    aspsps [--country NO]         list supported banks and card issuers in a country
    auth --aspsp NAME --redirect URL [--country NO] [--days 180] [--psu-type personal]
                                  start an authorisation and print the URL to open
    session --code CODE           exchange the code from the redirect for a session
    accounts --session ID         show each account's type and product name
    history --session ID | --account UID [--days 730]
                                  count transactions and show the date range available

See docs/enable-banking.md for the order to run them in.
"""

import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.enablebanking.com"

# Names that would mean a card issuer of interest is reachable by API.
CARD_KEYWORDS = ("seb", "eurocard", "sas", "mastercard", "american express", "amex")


def fail(message):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def config():
    app_id = os.environ.get("EB_APP_ID", "").strip()
    key_file = os.environ.get("EB_KEY_FILE", "").strip()
    if not app_id:
        fail("EB_APP_ID is not set")
    if not key_file:
        fail("EB_KEY_FILE is not set")
    if not os.path.isfile(key_file):
        fail(f"EB_KEY_FILE does not point to a file: {key_file}")
    mode = os.stat(key_file).st_mode & 0o077
    if mode:
        print(
            f"warning: {key_file} is readable by group or others; run: chmod 600 {key_file}",
            file=sys.stderr,
        )
    return app_id, key_file


def make_jwt():
    """RS256 token as Enable Banking expects it: kid = application id, valid one hour."""
    app_id, key_file = config()
    now = int(time.time())
    header = {"typ": "JWT", "alg": "RS256", "kid": app_id}
    claims = {
        "iss": "enablebanking.com",
        "aud": "api.enablebanking.com",
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = (
        b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + b64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    try:
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_file],
            input=signing_input.encode("ascii"),
            capture_output=True,
            check=True,
        )
    except FileNotFoundError:
        fail("the openssl command was not found")
    except subprocess.CalledProcessError as exc:
        fail(f"openssl could not sign with the key: {exc.stderr.decode(errors='replace').strip()}")
    return signing_input + "." + b64url(result.stdout)


def call(method, path, params=None, body=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", "Bearer " + make_jwt())
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        try:
            parsed = json.loads(detail)
            detail = parsed.get("message") or parsed.get("error") or detail
        except ValueError:
            pass
        fail(f"{method} {path} returned HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        fail(f"could not reach {API}: {exc.reason}")


def mask(value):
    value = str(value)
    return "…" + value[-4:] if len(value) > 4 else "…"


def cmd_selftest(_args):
    token = make_jwt()
    header, claims, signature = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(pad(header)))
    print("key file readable and signing works")
    print(f"token header: alg={decoded['alg']} kid={decoded['kid']}")
    print(f"signature length: {len(base64.urlsafe_b64decode(pad(signature)))} bytes")
    print("nothing was sent over the network")


def cmd_aspsps(args):
    data = call("GET", "/aspsps", params={"country": args.country})
    items = data.get("aspsps", [])
    if not items:
        fail(f"no ASPSPs returned for country {args.country}")
    print(f"{len(items)} entries for {args.country}:\n")
    matches = []
    for item in sorted(items, key=lambda i: i.get("name", "").lower()):
        name = item.get("name", "?")
        psu = ",".join(item.get("psu_types", [])) or "-"
        validity = item.get("maximum_consent_validity")
        days = f"{int(validity) // 86400} d" if isinstance(validity, (int, float)) else "-"
        print(f"  {name:<45} psu={psu:<20} max consent={days}")
        if any(keyword in name.lower() for keyword in CARD_KEYWORDS):
            matches.append(name)
    print()
    if matches:
        print("Possible card issuers found: " + "; ".join(matches))
    else:
        print("No entry matches SEB Kort, Eurocard, SAS, Mastercard or American Express.")
    print("Note: a sandbox application only lists banks that offer a sandbox.")
    print("The real list comes from a production application.")


def cmd_auth(args):
    valid_until = datetime.now(timezone.utc) + timedelta(days=args.days)
    state = secrets.token_hex(16)
    body = {
        "access": {"valid_until": valid_until.isoformat(timespec="seconds")},
        "aspsp": {"name": args.aspsp, "country": args.country},
        "state": state,
        "redirect_url": args.redirect,
        "psu_type": args.psu_type,
    }
    data = call("POST", "/auth", body=body)
    url = data.get("url")
    if not url:
        fail("the response contained no url")
    print("Open this URL in a browser and complete the bank login:\n")
    print("  " + url + "\n")
    print(f"Afterwards the browser is sent to {args.redirect}")
    print("with ?code=...&state=... in the address bar. The page itself does not need to load.")
    print(f"Check that state equals: {state}")
    print("Then run: eb_check.py session --code <the code value>")


def cmd_session(args):
    data = call("POST", "/sessions", body={"code": args.code})
    print(f"session id: {data.get('session_id', '?')}")
    access = data.get("access", {})
    if access.get("valid_until"):
        print(f"consent valid until: {access['valid_until']}")
    accounts = data.get("accounts", [])
    print(f"{len(accounts)} account(s) authorised:")
    if not accounts:
        print("  none. On a restricted production app this means the account was not linked.")
    for account in accounts:
        ident = account.get("account_id") or {}
        number = ident.get("iban") or (ident.get("other") or {}).get("identification") or ""
        if not number:
            for other in account.get("all_account_ids") or []:
                number = other.get("identification") or ""
                if number:
                    break
        label = account.get("name") or account.get("product") or account.get("cash_account_type") or "account"
        print(f"  uid={account.get('uid')}  {label}  {account.get('currency', '')}  {mask(number)}")
    print("\nRun within the hour: eb_check.py history --session " + str(data.get("session_id", "<session id>")))


def history_one(uid, date_from):
    base = {"strategy": "longest", "date_from": date_from}
    params = dict(base)
    total = 0
    booked = 0
    earliest = None
    latest = None
    pages = 0
    while True:
        data = call("GET", f"/accounts/{uid}/transactions", params=params)
        pages += 1
        for tx in data.get("transactions", []):
            total += 1
            if tx.get("status") == "BOOK":
                booked += 1
            date = tx.get("booking_date") or tx.get("value_date") or tx.get("transaction_date")
            if date:
                earliest = date if earliest is None or date < earliest else earliest
                latest = date if latest is None or date > latest else latest
        key = data.get("continuation_key")
        if not key or pages >= 500:
            break
        params = dict(base, continuation_key=key)
    return total, booked, earliest, latest, pages


def session_accounts(session_id):
    """Account uids in a session. The API may return uids or account objects."""
    data = call("GET", f"/sessions/{session_id}")
    uids = []
    for item in data.get("accounts", []):
        if isinstance(item, str):
            uids.append(item)
        elif isinstance(item, dict) and item.get("uid"):
            uids.append(item["uid"])
    if not uids:
        fail("the session lists no accounts")
    return uids


def cmd_accounts(args):
    uids = session_accounts(args.session)
    print(f"{len(uids)} account(s) in the session:\n")
    print(f"  {'account':<10} {'type':<6} {'product':<40} currency  number")
    for uid in uids:
        data = call("GET", f"/accounts/{uid}/details")
        ident = data.get("account_id") or {}
        number = ident.get("iban") or (ident.get("other") or {}).get("identification") or ""
        if not number:
            for other in data.get("all_account_ids") or []:
                number = other.get("identification") or ""
                if number:
                    break
        kind = data.get("cash_account_type") or "-"
        product = (data.get("product") or data.get("details") or data.get("usage") or "-")[:40]
        print(f"  {mask(uid):<10} {kind:<6} {product:<40} {data.get('currency', ''):<9} {mask(number)}")
    print("\nType CARD or a card-like product name marks a credit card account.")


def cmd_history(args):
    if not args.account and not args.session:
        fail("give --account UID or --session ID")
    date_from = (datetime.now(timezone.utc) - timedelta(days=args.days)).date().isoformat()
    uids = [args.account] if args.account else session_accounts(args.session)
    print(f"asked for transactions from {date_from}\n")
    print(f"  {'account':<10} {'total':>6} {'booked':>7}  {'earliest':<10}  {'latest':<10}  pages")
    for uid in uids:
        total, booked, earliest, latest, pages = history_one(uid, date_from)
        print(f"  {mask(uid):<10} {total:>6} {booked:>7}  {earliest or '-':<10}  {latest or '-':<10}  {pages}")
    print("\nAccounts are shown by the last four characters of their uid.")
    print("No transaction details were printed or stored.")


def main():
    parser = argparse.ArgumentParser(description="Checks against the Enable Banking API")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("selftest", help="check the key and token locally, no network")

    p = sub.add_parser("aspsps", help="list supported banks and card issuers")
    p.add_argument("--country", default="NO")

    p = sub.add_parser("auth", help="start an authorisation")
    p.add_argument("--aspsp", required=True, help="name exactly as printed by the aspsps command")
    p.add_argument("--redirect", required=True, help="a redirect URL registered on the application")
    p.add_argument("--country", default="NO")
    p.add_argument("--days", type=int, default=180, help="consent validity to request")
    p.add_argument("--psu-type", default="personal", choices=["personal", "business"])

    p = sub.add_parser("session", help="exchange the redirect code for a session")
    p.add_argument("--code", required=True)

    p = sub.add_parser("accounts", help="show the type and product of each account in a session")
    p.add_argument("--session", required=True)

    p = sub.add_parser("history", help="count transactions and show the available date range")
    p.add_argument("--account", help="one account uid from the session command")
    p.add_argument("--session", help="session id: check every account in the session")
    p.add_argument("--days", type=int, default=730, help="how far back to ask for (default 730)")

    args = parser.parse_args()
    {
        "selftest": cmd_selftest,
        "aspsps": cmd_aspsps,
        "auth": cmd_auth,
        "session": cmd_session,
        "accounts": cmd_accounts,
        "history": cmd_history,
    }[args.command](args)


if __name__ == "__main__":
    main()
