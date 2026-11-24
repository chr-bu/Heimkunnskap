"""From bank data to ledger rows. Pure functions, no database or network."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from decimal import Decimal, InvalidOperation

# Minor units per currency where it is not 2 (ISO 4217).
MINOR_DIGITS = {"JPY": 0, "ISK": 0, "KRW": 0, "CLP": 0, "HUF": 2}


def to_minor(amount, currency: str = "NOK") -> int:
    """'249.90' NOK -> 24990. Exact: Decimal, never float."""
    try:
        value = Decimal(str(amount).strip())
    except InvalidOperation:
        raise ValueError(f"not an amount: {amount!r}") from None
    digits = MINOR_DIGITS.get((currency or "NOK").upper(), 2)
    scaled = value.scaleb(digits)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{amount!r} has more decimals than {currency} allows")
    return int(scaled)


def digits(number: str | None) -> str:
    return re.sub(r"\D", "", number or "")


def bban_of(iban: str | None) -> str | None:
    """Norwegian IBAN NOkk BBBB CCCC CCK -> the 11-digit account number."""
    if not iban:
        return None
    compact = re.sub(r"\s", "", iban).upper()
    if compact.startswith("NO") and len(compact) == 15:
        return compact[4:]
    return None


def masked(number: str | None) -> str:
    d = digits(number)
    return "…" + d[-4:] if d else "…"


def matches_configured(configured: tuple, iban: str | None, bban: str | None) -> bool:
    """Is this account one of the configured numbers (BBAN or IBAN, any spacing)?"""
    have = {x for x in (digits(iban), digits(bban), digits(bban_of(iban))) if x}
    for item in configured:
        d = digits(item)
        if d and (d in have or any(h.endswith(d) and len(d) >= 11 for h in have)):
            return True
    return False


def account_identity(details: dict) -> dict:
    """The fields of an Enable Banking account resource that the ledger keeps.
    account_key is stable across sessions: the identification hash if
    present, else the IBAN or other number, else the uid."""
    ident = details.get("account_id") or {}
    iban = ident.get("iban")
    other = (ident.get("other") or {}).get("identification")
    if not other:
        for item in details.get("all_account_ids") or []:
            if item.get("identification"):
                other = item["identification"]
                break
    bban = bban_of(iban) or (digits(other) or None)
    key = (details.get("identification_hash")
           or (f"iban:{iban}" if iban else None)
           or (f"id:{other}" if other else None)
           or f"uid:{details.get('uid')}")
    return {
        "account_key": key,
        "uid": details.get("uid"),
        "name": details.get("name"),
        "product": details.get("product") or details.get("details"),
        "cash_account_type": details.get("cash_account_type"),
        "currency": (details.get("currency") or "NOK")[:3],
        "iban": iban,
        "bban": bban,
    }


ISO646_NO = str.maketrans("{|}[\\]", "æøåÆØÅ")


def fix_iso646(text: str | None) -> str | None:
    """Some banks (DNB) send counterparty names in 7-bit Norwegian, where { | } [ \\ ]
    stand for æ ø å Æ Ø Å: "L}nekassen" is "Lånekassen"."""
    return text.translate(ISO646_NO) if text else text


def _text(value) -> str:
    if isinstance(value, list):
        return " ".join(str(v).strip() for v in value if v)
    return str(value or "").strip()


def normalise_transaction(tx: dict, iso646_names: bool = False) -> dict | None:
    """One Enable Banking transaction as a ledger row, or None when it is
    not booked yet (pending ones change reference and amount).
    iso646_names: the bank sends names in 7-bit Norwegian (its profile says)."""
    if (tx.get("status") or "BOOK") != "BOOK":
        return None
    amount = tx.get("transaction_amount") or {}
    currency = (amount.get("currency") or "NOK").upper()
    minor = to_minor(amount.get("amount", "0"), currency)
    if (tx.get("credit_debit_indicator") or "").upper() == "DBIT":
        minor = -abs(minor)
    else:
        minor = abs(minor)
    booking = tx.get("booking_date") or tx.get("value_date") or tx.get("transaction_date")
    if not booking:
        raise ValueError("transaction without any date")
    outgoing = minor < 0
    party = (tx.get("creditor") if outgoing else tx.get("debtor")) or {}
    description = _text(tx.get("remittance_information")) or _text(party.get("name")) \
        or _text(tx.get("note")) or ""
    original_amount = original_currency = rate = None
    ex = tx.get("exchange_rate") or {}
    instructed = ex.get("instructed_amount") or {}
    if instructed.get("amount") and (instructed.get("currency") or "").upper() != currency:
        original_amount = Decimal(str(instructed["amount"]))
        original_currency = instructed["currency"].upper()
    if ex.get("exchange_rate"):
        try:
            rate = Decimal(str(ex["exchange_rate"]))
        except InvalidOperation:
            rate = None
    ref = tx.get("transaction_id") or tx.get("entry_reference")
    return {
        "booking_date": booking,
        "value_date": tx.get("value_date"),
        "amount_minor": minor,
        "currency": currency,
        "original_amount": original_amount,
        "original_currency": original_currency,
        "exchange_rate": rate,
        "description": description,
        "counterparty": (fix_iso646(_text(party.get("name"))) if iso646_names
                         else _text(party.get("name"))) or None,
        "external_ref": str(ref) if ref else None,
    }


# A payment reference (KID) that DNB puts in front of the text, and that
# does not always come back in another session: "600000006 Giro, Tibber".
LEADING_REFERENCE = re.compile(r"^[0-9][0-9-]* +")


def fingerprint_description(description: str) -> str:
    """The description as the fingerprint sees it. Must match the SQL in
    sql/006_fingerprints.sql, which recomputed the stored fingerprints."""
    return LEADING_REFERENCE.sub("", description or "")


def add_fingerprints(account_key: str, rows: list[dict]) -> list[dict]:
    """Fingerprint = sha256 of account, date, amount, description (without a
    leading payment reference) and an occurrence counter, so two identical
    purchases on one day stay two rows while the same purchase in an
    overlapping download, or seen through another person's consent, matches
    itself. Rows must cover whole days for the counter to be stable."""
    seen = Counter()
    for row in sorted(rows, key=lambda r: (r["booking_date"], r["amount_minor"],
                                           r["description"], r.get("external_ref") or "")):
        base = (account_key, str(row["booking_date"]), row["amount_minor"],
                fingerprint_description(row["description"]))
        seen[base] += 1
        text = "|".join(str(x) for x in base) + f"|{seen[base]}"
        row["fingerprint"] = hashlib.sha256(text.encode()).hexdigest()
    return rows


def balances_rows(data: dict, today: str) -> list[dict]:
    rows = []
    for b in data.get("balances") or []:
        amount = b.get("balance_amount") or {}
        currency = (amount.get("currency") or "NOK").upper()
        rows.append({
            "as_of": b.get("reference_date") or today,
            "balance_type": b.get("balance_type") or "unknown",
            "amount_minor": to_minor(amount.get("amount", "0"), currency),
            "currency": currency,
        })
    return rows
