import io
from datetime import date
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from heimkunnskap.cardfiles import (AMEX, DNB_MASTERCARD, CardFileError, detect, money, parse)

FIX = Path(__file__).parent / "fixtures"
AMEX_FILE = (FIX / "amex_sample.xlsx").read_bytes()
DNB_FILE = (FIX / "dnb_mastercard_sample.xlsx").read_bytes()


def edited(content: bytes, sheet: str, cell: str, value) -> bytes:
    wb = openpyxl.load_workbook(io.BytesIO(content))
    wb[sheet][cell] = value
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_detect():
    assert detect(AMEX_FILE) == AMEX
    assert detect(DNB_FILE) == DNB_MASTERCARD
    with pytest.raises(CardFileError, match="not an Excel file"):
        detect(b"Dato;Belop\n")
    wb = openpyxl.Workbook()
    wb.active.append(["Date", "Amount"])
    other = io.BytesIO()
    wb.save(other)
    with pytest.raises(CardFileError, match="not an Amex, DNB Mastercard or SAS Mastercard"):
        detect(other.getvalue())


def test_money_is_exact():
    assert money(312.75) == Decimal("312.75")
    assert money(2930.7500000000005) == Decimal("2930.75")
    with pytest.raises(CardFileError):
        money(1.234)


def test_amex():
    f = parse(AMEX_FILE)
    assert f.kind == AMEX and f.account_key == "file:amex:12345"
    assert "27 Oct, 2026 til 26 Nov, 2026" in f.period
    assert len(f.rows) == 5
    assert "match the summary sheet" in f.checks[0]
    first, cafe, airline, payment, sub = f.rows
    assert first["booking_date"] == date(2026, 11, 13)          # MM/DD, not DD/MM
    assert first["amount_minor"] == -49900
    assert first["external_ref"] == "AT000000000000000000001"
    assert cafe["original_amount"] == Decimal("26.50")
    assert cafe["original_currency"] == "EUR"
    assert cafe["exchange_rate"] == Decimal("11.518")
    assert cafe["extra"]["commission"] == "7.5"
    assert cafe["extra"]["country"] == "FRANCE"
    assert airline["card_holder"] == "KARI EKSEMPEL NORDMANN"
    # passenger name and ticket number never leave the raw file
    assert "NORDMANN/KARI" not in repr(airline) and "Billettnummer" not in repr(airline)
    assert payment["amount_minor"] == 250000                      # a payment is money in
    assert sub["booking_date"] == date(2026, 10, 28)


def test_amex_rejects_totals_that_do_not_match():
    bad = edited(AMEX_FILE, "Transaksjonssammendrag", "B11", 3000)
    with pytest.raises(CardFileError, match="do not match the summary"):
        parse(bad)


def test_amex_unknown_currency_is_kept_as_text():
    odd = edited(AMEX_FILE, "Transaksjonsdetaljer", "F9",
                 "Foreign Spend Amount: 100,00 KLINGON DARSEK Commission Amount: 2.0 "
                 "Currency Exchange Rate: 1.01")
    f = parse(odd)
    row = f.rows[1]
    assert row["original_amount"] == Decimal("100.00") and row["original_currency"] is None
    assert row["extra"]["currency_name"] == "KLINGON DARSEK"
    assert row["amount_minor"] == -31275                       # the NOK amount is what counts
    assert f.checks[-1] == "foreign amounts kept without a currency code for KLINGON DARSEK"


def test_amex_us_dollars():
    usd = edited(AMEX_FILE, "Transaksjonsdetaljer", "F9",
                 "Foreign Spend Amount: 31,95 UNITED STATES DOLLAR Commission Amount: 7.5 "
                 "Currency Exchange Rate: 9.554")
    assert parse(usd).rows[1]["original_currency"] == "USD"
    sgd = edited(AMEX_FILE, "Transaksjonsdetaljer", "F9",
                 "Foreign Spend Amount: 12,40 SINGAPORE DOLLAR Commission Amount: 3.2 "
                 "Currency Exchange Rate: 8.1")
    assert parse(sgd).rows[1]["original_currency"] == "SGD"


def test_amex_thousands_in_foreign_amount():
    big = edited(AMEX_FILE, "Transaksjonsdetaljer", "F9",
                 "Foreign Spend Amount: 1.026,50 EUROPEAN UNION EURO Commission Amount: 7.5 "
                 "Currency Exchange Rate: 11.518")
    assert parse(big).rows[1]["original_amount"] == Decimal("1026.50")


def test_dnb_mastercard():
    f = parse(DNB_FILE)
    assert f.kind == DNB_MASTERCARD and f.account_key == "file:dnb_mastercard"
    assert len(f.rows) == 10                                       # carried-over row skipped
    assert not any("Skyldig" in r["description"] for r in f.rows)
    ramen = f.rows[0]
    assert ramen["booking_date"] == date(2026, 11, 15)
    assert ramen["amount_minor"] == -13064
    assert (ramen["original_amount"], ramen["original_currency"]) == (Decimal("1840.00"), "JPY")
    assert ramen["exchange_rate"] == Decimal("0.071")
    refund = next(r for r in f.rows if r["amount_minor"] > 0)
    assert refund["amount_minor"] == 12000
    assert f.checks == ["3 foreign amounts agree with their rate"]
    assert f.period == "03.11.2026–15.11.2026"
    # two identical purchases stay two rows (no reference; fingerprints decide)
    assert sum(r["description"] == "TESTKAFE AS, BÆRUM" for r in f.rows) == 2


def test_dnb_foreign_amount_without_rate():
    f = parse(edited(edited(DNB_FILE, "transaksjonsliste", "C3", "93,33 EUR"),
                     "transaksjonsliste", "D3", None))
    ramen = f.rows[0]
    assert (ramen["original_amount"], ramen["original_currency"]) == (Decimal("93.33"), "EUR")
    assert ramen["exchange_rate"] is None and ramen["amount_minor"] == -13064


def test_dnb_rate_rounding_is_allowed():
    rounded = edited(edited(edited(DNB_FILE, "transaksjonsliste", "C3", "5800,00 JPY"),
                            "transaksjonsliste", "D3", 0.062), "transaksjonsliste", "F3", 362.04)
    assert parse(rounded).rows[0]["amount_minor"] == -36204
    big = edited(edited(edited(DNB_FILE, "transaksjonsliste", "C3", "12 500,00 JPY"),
                        "transaksjonsliste", "D3", 0.0624), "transaksjonsliste", "F3", 780.00)
    assert parse(big).rows[0]["original_amount"] == Decimal("12500.00")


def test_dnb_rejects_odd_foreign_amount_and_wrong_rate():
    with pytest.raises(CardFileError, match="foreign amount not understood"):
        parse(edited(DNB_FILE, "transaksjonsliste", "C3", "1840 yen"))
    with pytest.raises(CardFileError, match="is not 1840.00 JPY"):
        parse(edited(DNB_FILE, "transaksjonsliste", "D3", 0.08))


def test_sas_mastercard():
    from heimkunnskap.cardfiles import SAS_MASTERCARD
    from tests import sasfile
    content = sasfile.build()
    assert detect(content) == SAS_MASTERCARD
    card = parse(content)
    assert card.kind == SAS_MASTERCARD and card.period == "04.08.2025–22.10.2025"
    assert card.checks == ["3 section totals agree with their rows",
                           "2 foreign amounts agree with their rate"]
    by_text = {r["description"]: r for r in card.rows}
    assert len(card.rows) == 8
    paid = by_text["Innbetaling"]                                 # the later of two
    assert paid["amount_minor"] == 82057 and paid["card_holder"] is None
    assert by_text["Kortkontingent"]["amount_minor"] == -233500
    kafe = by_text["TESTKAFE AS"]
    assert (kafe["booking_date"], kafe["value_date"]) == (date(2025, 8, 4), date(2025, 8, 2))
    assert kafe["amount_minor"] == -150000 and kafe["card_holder"] == "Ola Eksempel"
    assert kafe["extra"] == {"place": "OSLO"}
    usd = by_text["PADDLE.NET* EKSEMPEL"]
    assert (usd["original_amount"], usd["original_currency"], usd["exchange_rate"]) == (
        Decimal("1.99"), "USD", Decimal("10.336683"))
    assert by_text["TOKYO EKSEMPEL"]["card_holder"] == "Kari Eksempel"
    assert by_text["SAS0000000000001"]["amount_minor"] == 81900          # a refund is money in


def test_sas_mastercard_checks_its_totals_and_rates():
    from tests import sasfile
    with pytest.raises(CardFileError, match="section total"):
        parse(sasfile.build(break_total=True))
    wrong_rate = [(*row[:7], row[7].replace("10.336683", "11.5")) if row[7] else row
                  for row in sasfile.OLA]
    with pytest.raises(CardFileError, match="is not 1.99 USD"):
        parse(sasfile.build(cards=(("540185******0001", "Ola Eksempel", wrong_rate),)))
