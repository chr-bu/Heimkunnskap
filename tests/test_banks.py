"""Bank profiles: each transaction is read with its own bank's wording.

Eksempelbanken is made up, with wording unlike DNB's, to show that the
pairing takes nothing for granted about the bank."""

import re
from datetime import date

import pytest

from heimkunnskap import banks, pairing
from heimkunnskap.banks import BankProfile, rules
from heimkunnskap.normalise import normalise_transaction

EKSEMPEL = BankProfile(
    name="Eksempelbanken",
    aspsp_names=("Eksempelbanken",),
    own_transfer=re.compile(r"^Egen overføring\b", re.I),
    card_bills=(("amex", re.compile(r"^Kortregning American Express\b", re.I)),),
    saving=rules(("saving:fund", r"^Fondskjøp\b")),
    saving_out=rules(("saving:sold", r"^Fondssalg\b")),
    dividend=re.compile(r"\bAvkastning\b", re.I),
    loans=rules(("loan:instalment", r"^Boliglån termin\b")),
)


@pytest.fixture(autouse=True)
def eksempelbanken(monkeypatch):
    monkeypatch.setattr(banks, "PROFILES", banks.PROFILES + [EKSEMPEL])


ACCOUNTS = {
    1: {"account_key": "e1", "aspsp_name": "Eksempelbanken", "cash_account_type": "CACC"},
    2: {"account_key": "e2", "aspsp_name": "Eksempelbanken", "cash_account_type": "CACC"},
    3: {"account_key": "d1", "aspsp_name": "DNB", "cash_account_type": "CACC"},
    4: {"account_key": "file:amex:99999", "aspsp_name": None, "cash_account_type": "CARD"},
    5: {"account_key": "x1", "aspsp_name": "Banken uten profil", "cash_account_type": "CACC"},
}
_ids = iter(range(1, 10_000))


def tx(account, day, kroner, text):
    return {"id": next(_ids), "account_id": account, "booking_date": date(2026, 10, day),
            "amount_minor": round(kroner * 100), "description": text}


def test_profiles_by_bank_name():
    assert banks.for_bank("DNB").name == "DNB"
    assert banks.for_bank("Eksempelbanken") is EKSEMPEL
    assert banks.for_bank("Banken uten profil") is banks.NO_PROFILE
    assert banks.for_bank(None) is banks.NO_PROFILE          # card accounts read from files


def test_each_bank_its_own_wording():
    a = tx(1, 5, -1000, "Egen overføring til sparekonto")
    b = tx(2, 5, 1000, "Egen overføring fra brukskonto")
    dnb_words = tx(1, 6, -500, "Kontoregulering, Overføring Mellom Egne Konti")   # DNB's, not this bank's
    bill = tx(1, 10, -4321, "Kortregning American Express oktober")
    paid = tx(4, 12, 4321, "BETALING MOTTATT, TAKK")
    fund = tx(1, 15, -2000, "Fondskjøp Globalindeks")
    sold = tx(2, 16, 3000, "Fondssalg Globalindeks")
    payout = tx(2, 17, 150, "Fondssalg Globalindeks, Avkastning")
    loan = tx(1, 20, -9000, "Boliglån termin 10/2026")
    out = pairing.classify([a, b, dnb_words, bill, paid, fund, sold, payout, loan], ACCOUNTS)
    assert out[a["id"]] == ("own_transfer", b["id"], "own:paired")
    assert dnb_words["id"] not in out
    assert out[bill["id"]] == ("card_payment", paid["id"], "card:amex")
    assert out[paid["id"]] == ("card_payment", bill["id"], "card:amex")
    assert out[fund["id"]] == ("saving", None, "saving:fund")
    assert out[sold["id"]] == ("saving", None, "saving:sold")
    assert payout["id"] not in out                            # a dividend is income
    assert out[loan["id"]] == ("loan", None, "loan:instalment")


def test_an_own_move_without_its_other_side():
    # Eksempelbanken names no hidden account: an own transfer without a partner
    lone = tx(1, 8, -700, "Egen overføring til konto i annen bank")
    # DNB's profile says the other side is the credit-line mortgage
    dnb = tx(3, 8, -700, "Kontoregulering, Overføring Mellom Egne Konti")
    out = pairing.classify([lone, dnb], ACCOUNTS)
    assert out[lone["id"]] == ("own_transfer", None, "own:unpaired")
    assert out[dnb["id"]] == ("loan", None, "loan:credit_line_transfer")


def test_a_bank_without_a_profile_gets_the_shared_rules_only():
    words = ["Kontoregulering, Overføring Mellom Egne Konti", "Giro, American Express",
             "Fondshandel", "Lån, Lån 1111.22.33333"]
    plain = [tx(5, 9, -100 - i, w) for i, w in enumerate(words)]
    student = tx(5, 9, -1500, "Giro, Statens Lånekasse")
    utlegg = tx(5, 9, 250, "Utlegg reise")
    out = pairing.classify(plain + [student, utlegg], ACCOUNTS)
    assert not any(t["id"] in out for t in plain)
    assert out[student["id"]] == ("loan", None, "loan:student")
    assert out[utlegg["id"]][0] == "reimbursement"


def test_loan_split_is_the_banks():
    text = "Lån, Lån 1111.22.33333, Avdrag Kr 6.000,00, Renter Kr 3.935,00, Omk. Kr 65,00"
    assert banks.for_bank("DNB").loan_split(text, -1_000_000) == (-600_000, -393_500, -6_500)
    assert EKSEMPEL.loan_split(text, -1_000_000) == (-1_000_000, 0, 0)    # no split known


def test_seven_bit_names_only_where_the_bank_sends_them():
    t = {"status": "BOOK", "transaction_amount": {"amount": "100", "currency": "NOK"},
         "credit_debit_indicator": "DBIT", "booking_date": "2026-10-01",
         "creditor": {"name": "Statens L}nekasse"}, "remittance_information": ["Giro"]}
    assert normalise_transaction(t, iso646_names=True)["counterparty"] == "Statens Lånekasse"
    assert normalise_transaction(t)["counterparty"] == "Statens L}nekasse"
