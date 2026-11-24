"""Parsers for card exports: American Express and the DNB Mastercard, which
have no API, and the SAS EuroBonus Mastercard, whose bank feed reaches back
only about 13 months. Layouts are documented in docs/formats/. Pure functions:
bytes in, rows out; no database.

A file that does not look exactly as documented is rejected with a reason
rather than half-read. Each new surprise (a currency, a column) is then
added once, from a real file.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .normalise import to_minor

AMEX = "amex"
DNB_MASTERCARD = "dnb_mastercard"
SAS_MASTERCARD = "sas_mastercard"

# Amex writes currencies as English names. A name not listed here does not
# stop the import: the NOK amount is what counts, and the foreign amount is
# kept with the name as text (extra.currency_name). The upload result names
# it, so it can be added here.
AMEX_CURRENCIES = {
    "EUROPEAN UNION EURO": "EUR",          # seen
    "UNITED STATES DOLLAR": "USD",         # seen
    "SINGAPORE DOLLAR": "SGD",             # seen
    # Others, following the same pattern
    "SWEDISH KRONA": "SEK",
    "DANISH KRONE": "DKK",
    "POUND STERLING": "GBP",
    "SWISS FRANC": "CHF",
    "JAPANESE YEN": "JPY",
    "ICELAND KRONA": "ISK",
    "POLISH ZLOTY": "PLN",
    "CZECH KORUNA": "CZK",
    "HUNGARIAN FORINT": "HUF",
    "CANADIAN DOLLAR": "CAD",
    "AUSTRALIAN DOLLAR": "AUD",
    "THAI BAHT": "THB",
    "TURKISH LIRA": "TRY",
    "HONG KONG DOLLAR": "HKD",
    "NEW ZEALAND DOLLAR": "NZD",
    "MALAYSIAN RINGGIT": "MYR",
    "INDONESIAN RUPIAH": "IDR",
    "SOUTH AFRICAN RAND": "ZAR",
    "MEXICAN PESO": "MXN",
    "UAE DIRHAM": "AED",
    "CHINESE YUAN RENMINBI": "CNY",
    "KOREAN WON": "KRW",
    "INDIAN RUPEE": "INR",
}

DNB_CARRIED_OVER = "Skyldig beløp fra forrige faktura"


class CardFileError(ValueError):
    """The file is not a card export we understand, or fails its own checks."""


@dataclass
class CardFile:
    kind: str                       # AMEX, DNB_MASTERCARD or SAS_MASTERCARD
    account_key: str                # stable across uploads
    account_name: str
    rows: list[dict]
    period: str | None = None       # as the file states it
    checks: list[str] = field(default_factory=list)   # what was verified


def _workbook(content: bytes):
    import openpyxl
    try:
        return openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise CardFileError(f"not an Excel file ({type(exc).__name__})") from None


def _rows(ws) -> list[tuple]:
    return [tuple(r) for r in ws.iter_rows(values_only=True)]


def _text(value) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def money(value) -> Decimal:
    """A cell holding a kroner amount as an exact Decimal with two decimals.
    Excel stores floats; anything further than rounding noise from whole
    øre is refused."""
    if value is None or value == "":
        raise CardFileError("empty amount")
    try:
        d = Decimal(str(value).replace(" ", ""))
    except InvalidOperation:
        raise CardFileError(f"not an amount: {value!r}") from None
    q = d.quantize(Decimal("0.01"))
    if abs(d - q) > Decimal("0.000001"):
        raise CardFileError(f"amount with more than two decimals: {value!r}")
    return q


def detect(content: bytes) -> str:
    wb = _workbook(content)
    names = wb.sheetnames
    try:
        if "Transaksjonsdetaljer" in names:
            rows = _rows(wb["Transaksjonsdetaljer"])
            if len(rows) >= 7 and _text(rows[6][0]) == "Dato" and "Referanse" in map(_text, rows[6]):
                return AMEX
        if "transaksjonsliste" in names:
            header = [_text(c) for c in _rows(wb["transaksjonsliste"])[0]]
            if header[:6] == ["Dato", "Beløpet gjelder", "Valuta", "Kurs", "Inn", "Ut"]:
                return DNB_MASTERCARD
        if "Transaksjoner" in names:
            rows = _rows(wb["Transaksjoner"])
            if (rows and _text(rows[0][0]) == "Transaksjonseksport"
                    and any([_text(c) for c in r[:7]] == SAS_HEADER for r in rows[:30])):
                return SAS_MASTERCARD
    finally:
        wb.close()
    raise CardFileError("not an Amex, DNB Mastercard or SAS Mastercard export "
                        "(unknown sheets or headers)")


def parse(content: bytes) -> CardFile:
    kind = detect(content)
    return {AMEX: parse_amex, DNB_MASTERCARD: parse_dnb_mastercard,
            SAS_MASTERCARD: parse_sas_mastercard}[kind](content)


# -- American Express

FOREIGN = re.compile(
    r"Foreign Spend Amount:\s*(?P<amount>[\d.,]+)\s+(?P<currency>.+?)\s+"
    r"Commission Amount:\s*(?P<commission>[\d.,]+)\s+"
    r"Currency Exchange Rate:\s*(?P<rate>[\d.,]+)", re.S)


def _amex_foreign(details: str) -> dict | None:
    """The foreign-spend line from 'Utvidede detaljer', or None. Only these
    fields are taken: the rest of that column can hold passenger names and
    ticket numbers, which stay in the raw store."""
    if "Foreign Spend Amount" not in details:
        return None
    m = FOREIGN.search(details)
    if not m:
        raise CardFileError(f"unreadable foreign spend line: {details[:80]!r}")
    name = " ".join(m["currency"].split()).upper()
    # Norwegian notation in the amount (9.999,99), a decimal point in the others
    amount = Decimal(m["amount"].replace(".", "").replace(",", "."))
    return {"original_amount": amount, "original_currency": AMEX_CURRENCIES.get(name),
            "currency_name": name, "exchange_rate": Decimal(m["rate"]),
            "commission": str(Decimal(m["commission"]))}


def _amex_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", _text(value))
    if not m:
        raise CardFileError(f"Amex date not MM/DD/YYYY: {value!r}")
    return date(int(m[3]), int(m[1]), int(m[2]))


def parse_amex(content: bytes) -> CardFile:
    wb = _workbook(content)
    try:
        rows = _rows(wb["Transaksjonsdetaljer"])
        summary = _rows(wb["Transaksjonssammendrag"]) if "Transaksjonssammendrag" in wb.sheetnames else []
    finally:
        wb.close()
    period = _text(rows[0][1]) if len(rows[0]) > 1 else None
    account_no = _text(rows[4][0])
    m = re.fullmatch(r"X{4}-X{6}-(\d{5})", account_no)
    if not m:
        raise CardFileError(f"Amex account number not in the expected masked form: {account_no!r}")
    header = [_text(c) for c in rows[6]]
    col = {name: header.index(name) for name in
           ("Dato", "Beskrivelse", "Kortmedlem", "Kontonummer", "Beløp", "Utvidede detaljer",
            "Sted", "Land", "Referanse", "Kategori") if name in header}
    missing = {"Dato", "Beskrivelse", "Kortmedlem", "Beløp", "Referanse"} - col.keys()
    if missing:
        raise CardFileError(f"Amex columns missing: {', '.join(sorted(missing))}")

    out, charges, credits, unknown = [], Decimal(0), Decimal(0), set()
    for r in rows[7:]:
        if not any(c not in (None, "") for c in r):
            continue
        get = lambda name: r[col[name]] if name in col and col[name] < len(r) else None
        amount = money(get("Beløp"))
        if amount > 0:
            charges += amount
        else:
            credits += amount
        ref = _text(get("Referanse"))
        if not ref:
            raise CardFileError("Amex row without a reference")
        extra = {"card_number": _text(get("Kontonummer")) or None,
                 "town": _text(get("Sted")) or None, "country": _text(get("Land")) or None,
                 "category": _text(get("Kategori")) or None}
        foreign = _amex_foreign(str(get("Utvidede detaljer") or ""))
        if foreign:
            extra["commission"] = foreign.pop("commission")
            name = foreign.pop("currency_name")
            if foreign["original_currency"] is None:
                extra["currency_name"] = name
                unknown.add(name)
        description = _text(get("Beskrivelse"))
        out.append({
            "booking_date": _amex_date(get("Dato")),
            "value_date": None,
            "amount_minor": -to_minor(amount),          # a charge is money out
            "currency": "NOK",
            "original_amount": foreign and foreign["original_amount"],
            "original_currency": foreign and foreign["original_currency"],
            "exchange_rate": foreign and foreign["exchange_rate"],
            "description": description,
            "counterparty": description or None,
            "external_ref": ref,
            "card_holder": _text(get("Kortmedlem")) or None,
            "extra": {k: v for k, v in extra.items() if v is not None},
        })
    if not out:
        raise CardFileError("no transactions in the Amex file")

    checks = []
    totals = {_text(r[0]): r[1] for r in summary if r and len(r) > 1 and r[0]}
    if "Debiteringer" in totals and "Betalinger og kreditter" in totals:
        want_charges = money(totals["Debiteringer"])
        want_credits = money(totals["Betalinger og kreditter"])
        if charges != want_charges or credits != want_credits:
            raise CardFileError(
                f"Amex totals do not match the summary sheet: charges {charges} against "
                f"{want_charges}, payments and credits {credits} against {want_credits}")
        checks.append(f"charges {charges} and payments {credits} match the summary sheet")
    else:
        checks.append("no summary sheet to check against")
    if unknown:
        checks.append("foreign amounts kept without a currency code for "
                      + ", ".join(sorted(unknown)))
    return CardFile(kind=AMEX, account_key=f"file:amex:{m[1]}",
                    account_name="American Express", rows=out, period=period, checks=checks)


# -- DNB Mastercard

# "1840,00 JPY", possibly with thousands separators ("12 345,00 JPY")
VALUTA = re.compile(r"(?P<amount>\d{1,3}(?:[ .]\d{3})+(?:,\d+)?|\d+(?:,\d+)?) (?P<code>[A-Z]{3})")
# DNB's rate is NOK per unit, rounded (0.062 for yen): the check allows for
# that rounding plus a small margin
RATE_MARGIN = Decimal("0.003")


def _dnb_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()                        # stored at 01:00; the time is noise
    if isinstance(value, date):
        return value
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", _text(value))
    if not m:
        raise CardFileError(f"DNB Mastercard date not understood: {value!r}")
    return date(int(m[3]), int(m[2]), int(m[1]))


def parse_dnb_mastercard(content: bytes) -> CardFile:
    wb = _workbook(content)
    try:
        rows = _rows(wb["transaksjonsliste"])
    finally:
        wb.close()
    out, rate_checked = [], 0
    for r in rows[1:]:
        r = tuple(r) + (None,) * (6 - len(r))
        dato, text, valuta, kurs, inn, ut = r[:6]
        description = _text(text)
        if not description and dato is None and inn is None and ut is None:
            continue
        if description == DNB_CARRIED_OVER:
            continue
        if (inn is None) == (ut is None):
            raise CardFileError(f"DNB Mastercard row with both or neither of Inn and Ut: {description!r}")
        amount = money(inn) if inn is not None else -money(ut)
        original_amount = original_currency = rate = None
        if valuta not in (None, ""):
            m = VALUTA.fullmatch(_text(valuta))
            if not m:
                raise CardFileError(f"DNB Mastercard foreign amount not understood: {valuta!r}")
            original_amount = Decimal(re.sub(r"[ .]", "", m["amount"]).replace(",", "."))
            original_currency = m["code"]
            if kurs not in (None, ""):
                # Some rows have no rate (seen for a euro purchase): the NOK
                # amount stands, and the rate is left empty
                try:
                    rate = Decimal(_text(kurs).replace(",", "."))
                except InvalidOperation:
                    raise CardFileError(f"DNB Mastercard rate not understood: {kurs!r}") from None
                half_unit = Decimal(1).scaleb(rate.as_tuple().exponent) / 2
                expected = original_amount * rate
                allowed = original_amount * half_unit + abs(amount) * RATE_MARGIN + Decimal("0.01")
                if abs(abs(amount) - expected) > allowed:
                    raise CardFileError(
                        f"DNB Mastercard amount {abs(amount)} is not {original_amount} "
                        f"{original_currency} × {rate}: {description!r}")
                rate_checked += 1
        out.append({
            "booking_date": _dnb_date(dato),
            "value_date": None,
            "amount_minor": to_minor(amount),
            "currency": "NOK",
            "original_amount": original_amount,
            "original_currency": original_currency,
            "exchange_rate": rate,
            "description": description,
            "counterparty": description.split(",")[0].strip() or None,
            "external_ref": None,                  # none in the file: fingerprints
            "card_holder": None,
            "extra": None,
        })
    if not out:
        raise CardFileError("no transactions in the DNB Mastercard file")
    checks = [f"{rate_checked} foreign amounts agree with their rate"] if rate_checked else []
    dates = sorted(r["booking_date"] for r in out)
    return CardFile(kind=DNB_MASTERCARD, account_key="file:dnb_mastercard",
                    account_name="DNB Mastercard", rows=out,
                    period=f"{dates[0]:%d.%m.%Y}–{dates[-1]:%d.%m.%Y}", checks=checks)


# -- SAS EuroBonus Mastercard (SEB Kort)

SAS_HEADER = ["Dato", "Bokført", "Spesifikasjon", "Sted", "Valuta", "Utl. beløp", "Beløp"]
SAS_CARD = re.compile(r"^\d{6}\*+\d{4}$")                     # 540185******1234
SAS_RATE = re.compile(r"^Valutakurs: (?P<rate>\d+(?:\.\d+)?)\b")


def _sas_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(_text(value), "%d.%m.%Y").date()
    except ValueError:
        raise CardFileError(f"SAS Mastercard date not understood: {value!r}") from None


def parse_sas_mastercard(content: bytes) -> CardFile:
    """One sheet, "Transaksjoner": first the account's own events (payments
    in, fees), then one section per card headed by the masked card number and
    the holder's name. Purchases are positive, payments negative; the ledger
    keeps the bank's sign (money out negative). A foreign purchase is
    followed by a line "Valutakurs: <rate> ...", markup included. Every
    section ends with "Totalbeløp", checked against its rows; the account
    section's total also holds the card sections' sum ("Saldo hendelser")."""
    wb = _workbook(content)
    try:
        rows = _rows(wb["Transaksjoner"])
    finally:
        wb.close()
    out: list[dict] = []
    holder = None
    section: list[Decimal] = []
    sections = rate_checked = 0
    for r in rows[1:]:
        r = tuple(r) + (None,) * (7 - len(r))
        first, cells = r[0], [_text(c) for c in r[:7]]
        if cells == SAS_HEADER or cells[0] in ("Totalt andre hendelser", "Kjøp/uttak"):
            continue
        if SAS_CARD.match(cells[0]):
            holder = cells[1] or None
            continue
        if cells[2] == "Saldo hendelser":
            section.append(money(r[6]))
            continue
        if cells[0] == "Totalbeløp":
            if sum(section, Decimal(0)) != money(r[6]):
                raise CardFileError(f"SAS Mastercard section total {money(r[6])} does not "
                                    f"match its rows ({sum(section, Decimal(0))})")
            section, sections = [], sections + 1
            continue
        m = SAS_RATE.match(cells[0])
        if m:
            if not out or out[-1]["original_amount"] is None:
                raise CardFileError(f"SAS Mastercard exchange rate with no foreign amount: {cells[0]!r}")
            last = out[-1]
            last["exchange_rate"] = Decimal(m["rate"])
            expected = last["original_amount"] * last["exchange_rate"]
            if abs(abs(last["amount_minor"]) / 100 - float(expected)) > 0.01 + float(expected) * 0.003:
                raise CardFileError(f"SAS Mastercard amount {abs(last['amount_minor']) / 100} is not "
                                    f"{last['original_amount']} {last['original_currency']} "
                                    f"× {last['exchange_rate']}: {last['description']!r}")
            rate_checked += 1
            continue
        if not any(cells):
            continue
        if not isinstance(first, (date, datetime)) and not re.match(r"\d{2}\.\d{2}\.\d{4}$", cells[0]):
            raise CardFileError(f"SAS Mastercard row not understood: {cells[:3]!r}")
        amount = money(r[6])
        section.append(amount)
        currency = cells[4] or "NOK"
        foreign = currency != "NOK"
        if foreign and not re.fullmatch(r"[A-Z]{3}", currency):
            raise CardFileError(f"SAS Mastercard currency not understood: {currency!r}")
        description = cells[2]
        out.append({
            "booking_date": _sas_date(r[1]),
            "value_date": _sas_date(first),            # the purchase date
            "amount_minor": -to_minor(amount),
            "currency": "NOK",
            "original_amount": Decimal(str(r[5])) if foreign else None,
            "original_currency": currency if foreign else None,
            "exchange_rate": None,
            "description": description,
            "counterparty": description or None,
            "external_ref": None,                  # none in the file: fingerprints
            "card_holder": holder,
            "extra": {"place": cells[3]} if cells[3] else None,
        })
    if section:
        raise CardFileError("SAS Mastercard file ends without a total: cut off?")
    if not out:
        raise CardFileError("no transactions in the SAS Mastercard file")
    checks = [f"{sections} section totals agree with their rows"]
    if rate_checked:
        checks.append(f"{rate_checked} foreign amounts agree with their rate")
    dates = sorted(r["booking_date"] for r in out)
    return CardFile(kind=SAS_MASTERCARD, account_key="file:sas_mastercard",
                    account_name="SAS EuroBonus Mastercard", rows=out,
                    period=f"{dates[0]:%d.%m.%Y}–{dates[-1]:%d.%m.%Y}", checks=checks)
