from decimal import Decimal

import pytest

from heimkunnskap.normalise import (account_identity, add_fingerprints, balances_rows, bban_of,
                                    masked, matches_configured, normalise_transaction, to_minor)


def test_to_minor_exact():
    assert to_minor("249.90") == 24990
    assert to_minor("-0.01") == -1
    assert to_minor(" 1000 ") == 100000
    assert to_minor("1234", "JPY") == 1234
    with pytest.raises(ValueError):
        to_minor("1.234")
    with pytest.raises(ValueError):
        to_minor("12.5", "JPY")
    with pytest.raises(ValueError):
        to_minor("abc")


def test_bban_and_masking():
    assert bban_of("NO93 8601 1117 947") == "86011117947"
    assert bban_of("DE89370400440532013000") is None
    assert masked("8601.11.17947") == "…7947"
    assert masked(None) == "…"


def test_matches_configured_any_spelling():
    configured = ("8601.11.17947",)
    assert matches_configured(configured, "NO9386011117947", None)
    assert matches_configured(configured, None, "86011117947")
    assert not matches_configured(configured, None, "12345678903")
    assert not matches_configured((), None, "86011117947")
    # a short fragment never matches by suffix
    assert not matches_configured(("7947",), None, "86011117947")


def test_account_identity_prefers_hash():
    ident = account_identity({
        "uid": "u-1", "identification_hash": "h-abc",
        "account_id": {"iban": "NO9386011117947"}, "cash_account_type": "CACC",
        "product": "Brukskonto", "currency": "NOK"})
    assert ident["account_key"] == "h-abc"
    assert ident["bban"] == "86011117947"
    assert ident["product"] == "Brukskonto"


def test_account_identity_falls_back_to_numbers():
    ident = account_identity({"uid": "u-2", "all_account_ids": [
        {"identification": "540185******7842", "scheme_name": "CPAN"}]})
    assert ident["account_key"] == "id:540185******7842"
    assert account_identity({"uid": "u-3"})["account_key"] == "uid:u-3"


def tx(**kw):
    base = {"transaction_amount": {"amount": "249.90", "currency": "NOK"},
            "credit_debit_indicator": "DBIT", "status": "BOOK",
            "booking_date": "2026-11-02", "value_date": "2026-11-02",
            "remittance_information": ["Varekjøp", "REMA 1000"],
            "creditor": {"name": "REMA 1000"}, "entry_reference": "E1"}
    base.update(kw)
    return base


def test_normalise_debit_and_credit():
    row = normalise_transaction(tx())
    assert row["amount_minor"] == -24990
    assert row["description"] == "Varekjøp REMA 1000"
    assert row["counterparty"] == "REMA 1000"
    assert row["external_ref"] == "E1"
    credit = normalise_transaction(tx(credit_debit_indicator="CRDT",
                                      transaction_amount={"amount": "-100", "currency": "NOK"},
                                      debtor={"name": "Arbeidsgiver AS"}, creditor=None))
    assert credit["amount_minor"] == 10000
    assert credit["counterparty"] == "Arbeidsgiver AS"


def test_normalise_skips_pending_and_prefers_transaction_id():
    assert normalise_transaction(tx(status="PDNG")) is None
    assert normalise_transaction(tx(transaction_id="T9"))["external_ref"] == "T9"
    assert normalise_transaction(tx(entry_reference=None))["external_ref"] is None


def test_normalise_foreign_currency():
    row = normalise_transaction(tx(exchange_rate={
        "unit_currency": "JPY", "exchange_rate": "0.0712",
        "instructed_amount": {"amount": "3500", "currency": "JPY"}}))
    assert row["original_amount"] == Decimal("3500")
    assert row["original_currency"] == "JPY"
    assert row["exchange_rate"] == Decimal("0.0712")


def test_fingerprints_keep_identical_purchases_apart_and_are_stable():
    a = [normalise_transaction(tx(entry_reference=None)) for _ in range(2)]
    add_fingerprints("acct", a)
    assert a[0]["fingerprint"] != a[1]["fingerprint"]
    # the same day downloaded again, with another day around it
    b = [normalise_transaction(tx(entry_reference=None, booking_date="2026-11-01")),
         *[normalise_transaction(tx(entry_reference=None)) for _ in range(2)]]
    add_fingerprints("acct", b)
    assert {r["fingerprint"] for r in a} <= {r["fingerprint"] for r in b}
    # another account never collides
    c = add_fingerprints("other", [normalise_transaction(tx(entry_reference=None))])
    assert c[0]["fingerprint"] not in {r["fingerprint"] for r in a}


def test_balances():
    rows = balances_rows({"balances": [
        {"balance_amount": {"amount": "-1520.50", "currency": "NOK"},
         "balance_type": "CLBD", "reference_date": "2026-11-14"},
        {"balance_amount": {"amount": "10", "currency": "NOK"}, "balance_type": "ITAV"}]},
        "2026-11-15")
    assert rows[0] == {"as_of": "2026-11-14", "balance_type": "CLBD",
                       "amount_minor": -152050, "currency": "NOK"}
    assert rows[1]["as_of"] == "2026-11-15"
