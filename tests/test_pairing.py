from datetime import date

from heimkunnskap.banks.dnb import loan_split
from heimkunnskap.pairing import classify

ACCOUNTS = {
    1: {"account_key": "h1", "aspsp_name": "DNB", "cash_account_type": "CACC",      # main
        "name": "Nordmann Ola Eksempel"},
    2: {"account_key": "h2", "aspsp_name": "DNB", "cash_account_type": "CACC"},     # Kari
    3: {"account_key": "h3", "aspsp_name": "SAS EuroBonus Mastercard", "cash_account_type": "CARD"},
    4: {"account_key": "file:amex:12345", "aspsp_name": None, "cash_account_type": "CARD"},
    5: {"account_key": "file:dnb_mastercard", "aspsp_name": None, "cash_account_type": "CARD"},
}
OWN = "Kontoregulering, Overføring Mellom Egne Konti"
_ids = iter(range(1, 10_000))


def tx(account, day, kroner, text):
    return {"id": next(_ids), "account_id": account, "booking_date": date(2026, 10, day),
            "amount_minor": round(kroner * 100), "description": text}


def test_own_transfers_pair_by_amount_and_date():
    a = tx(1, 20, -50000, OWN)
    b = tx(2, 20, 50000, OWN)
    c = tx(1, 23, -35, OWN)
    d = tx(2, 24, 35, OWN)          # a day later still pairs
    e = tx(2, 20, -2500, OWN)       # to Kari's savings, not visible: no partner
    f = tx(1, 20, 50000, "Lønn")    # same amount, not a transfer
    out = classify([a, b, c, d, e, f], ACCOUNTS)
    assert out[a["id"]] == ("own_transfer", b["id"], "own:paired")
    assert out[b["id"]] == ("own_transfer", a["id"], "own:paired")
    assert out[c["id"]][1] == d["id"]
    # its other side is the rammelån, which the bank does not share
    assert out[e["id"]] == ("loan", None, "loan:credit_line_transfer")
    assert f["id"] not in out


def test_two_equal_transfers_pair_one_to_one():
    rows = [tx(1, 23, -35, OWN), tx(2, 23, 35, OWN), tx(1, 23, -35, OWN), tx(2, 23, 35, OWN)]
    out = classify(rows, ACCOUNTS)
    partners = [out[r["id"]][1] for r in rows]
    assert None not in partners and len(set(partners)) == 4


def test_card_bills_pair_with_the_right_card():
    sas_bill = tx(1, 20, -19.07, "300000000000003 Giro, Seb Kort, Efaktura")
    sas_paid = tx(3, 20, 19.07, "Inbetalning")
    amex_bill = tx(1, 11, -3082.78, "100000000000001 Giro, American Express, Efaktura")
    amex_paid = tx(4, 12, 3082.78, "BETALING MOTTATT - TAKK")
    dnb_bill = tx(1, 11, -76740.91, "200000000000002 Giro, Dnb Bank Asa, Efaktura")
    refund = tx(5, 14, 120, "PRØVEBUTIKK, BERGEN")      # a refund is not a payment
    out = classify([sas_bill, sas_paid, amex_bill, amex_paid, dnb_bill, refund], ACCOUNTS)
    assert out[sas_bill["id"]] == ("card_payment", sas_paid["id"], "card:sas")
    assert out[sas_paid["id"]] == ("card_payment", sas_bill["id"], "card:sas")
    assert out[amex_paid["id"]][1] == amex_bill["id"]
    # the DNB Mastercard file is not uploaded for that period: still a card bill
    assert out[dnb_bill["id"]] == ("card_payment", None, "card:dnb_mastercard")
    assert refund["id"] not in out


def test_a_card_bill_never_pairs_with_another_card():
    amex_bill = tx(1, 11, -500, "Giro, American Express, Efaktura")
    sas_credit = tx(3, 11, 500, "Inbetalning")
    out = classify([amex_bill, sas_credit], ACCOUNTS)
    assert out[amex_bill["id"]] == ("card_payment", None, "card:amex")
    assert out[sas_credit["id"]] == ("card_payment", None, "card:credit_unmatched")


def test_saving_and_loans():
    fund = tx(1, 2, -300, "500000000005 Giro, Dnb Bank Asa, Fondshandel")
    loan = tx(1, 20, -10000, "Lån, Lån 1111.22.33333, Avdrag Kr 6.000,00, Renter Kr 3.935,00")
    line = tx(1, 23, -53000, "Overføring Innland, Rammelån, Nettgiro")
    house = tx(1, 24, -45000, "Overføring Innland, Huslån, Nettgiro")
    trumf = tx(1, 30, -6273.34, "400000000004 Giro, Norgesgruppen Trumf, Efaktura")
    student = tx(1, 20, -1885, "800000008 Giro, Statens Lånekasse For Utdannin, Avtalegiro")
    student2 = tx(1, 20, -2270, "900000- Giro, Lånekassen, Efaktura")
    out = classify([fund, loan, line, house, trumf, student, student2], ACCOUNTS)
    assert out[house["id"]] == ("loan", None, "loan:credit_line")
    assert out[student["id"]] == ("loan", None, "loan:student")
    assert out[student2["id"]] == ("loan", None, "loan:student")
    car = tx(1, 19, -5928, "Giro, Dnb Finans, Efaktura")
    assert classify([car], ACCOUNTS)[car["id"]] == ("loan", None, "loan:car")
    shares = tx(1, 15, -100000, "Giro, Eksempel Employee Invest AS, Nettgiro")
    dividend = tx(1, 30, 29105.46, "Overføring Innland, Eksempel Employee Invest As")
    out2 = classify([shares, dividend], ACCOUNTS)
    assert out2[shares["id"]] == ("saving", None, "saving:employee_shares")
    assert dividend["id"] not in out2              # a dividend is income
    draw = tx(2, 4, 150000, "Lån, Eksempel Kari")              # paid out of the rammelån
    back = tx(1, 6, -20000, "Lån, Eksempel Kari")
    out3 = classify([draw, back], ACCOUNTS)
    assert out3[draw["id"]] == ("loan", None, "loan:payout")
    assert out3[back["id"]] == ("loan", None, "loan:payout")
    assert out[fund["id"]] == ("saving", None, "saving:fund")
    assert out[loan["id"]] == ("loan", None, "loan:instalment")
    assert out[line["id"]] == ("loan", None, "loan:credit_line")
    # the Trumf card's bill: no card data, so it stands for its purchases
    assert out[trumf["id"]] == ("card_payment", None, "card:trumf")


def test_loan_split():
    text = ("Lån, Lån 1111.22.33333, Avdrag      Kr       6.000,00, Renter      Kr       "
            "3.935,00, Omk.        Kr          65,00")
    assert loan_split(text, -1000000) == (-600000, -393500, -6500)
    interest_only = ("Lån, Lån 4444.55.66666, Avdrag      Kr           0,00, Renter      Kr      "
                     "21.500,00, Omk.        Kr          65,00")
    assert loan_split(interest_only, -2156500) == (0, -2150000, -6500)
    # parts that do not add up: all principal, nothing guessed
    assert loan_split(text, -998000) == (-998000, 0, 0)
    # From November 2026: a reference after "Lån", no fee in the text; the remainder is the fee
    new = "Lån  123456, Lån 1111.22.33333, Avdrag Kr 6.000,00, Renter Kr 3.935,00"
    assert loan_split(new, -1000000) == (-600000, -393500, -6500)
    assert loan_split(new, -993500) == (-600000, -393500, 0)
    assert loan_split(new, -1100000) == (-1100000, 0, 0)        # 1,065 kr left: not a fee
    out = classify([tx(1, 20, -10000, new)], ACCOUNTS)
    assert list(out.values()) == [("loan", None, "loan:instalment")]
    assert loan_split("Overføring Innland, Rammelån, Nettgiro", -5300000) == (-5300000, 0, 0)
    assert loan_split("Kontoregulering, Overføring Mellom Egne Konti", 3050000) == (3050000, 0, 0)


def test_reimbursed_expenses():
    laptop = tx(1, 1, -12990.00, "POWER.no / 100000001")
    paid_back = tx(1, 20, 12990.00, "utlegg ABCD Overføring Innland, Eksempel Consulting AS")
    other = tx(1, 25, 2500.00, "utlegg ABCD Overføring Innland, Eksempel Consulting AS")
    fee = tx(1, 25, 40000, "20 timer Overføring Innland, Eksempel Consulting AS")
    out = classify([laptop, paid_back, other, fee], ACCOUNTS)
    assert out[paid_back["id"]] == ("reimbursement", laptop["id"], "reimbursement:utlegg")
    assert out[other["id"]] == ("reimbursement", None, "reimbursement:utlegg")
    assert laptop["id"] not in out and fee["id"] not in out     # spending and income stay


def test_electricity_support_lowers_a_cost():
    support = tx(1, 19, 1119.86, "Faktureringsavtaleid: 1 Overføring Innland, Elvia As")
    out = classify([support], ACCOUNTS)
    assert out[support["id"]] == ("reimbursement", None, "reimbursement:electricity_support")


def test_money_lent_and_repaid():
    from heimkunnskap.pairing import lent
    out_ = {"id": 900, "booking_date": date(2024, 12, 9), "amount_minor": -43000000,
            "description": "Overføring Innland, Eksempel Holding As, Nettgiro"}
    equity = {"id": 901, "booking_date": date(2024, 9, 13), "amount_minor": -3000000,
              "description": "Overføring Innland, Eksempel Holding As, Nettgiro"}
    early = {"id": 902, "booking_date": date(2024, 11, 1), "amount_minor": 50000,
             "description": "Overføring Innland, Eksempel Holding As"}    # before the loan
    backs = [{"id": 903 + i, "booking_date": d, "amount_minor": a,
              "description": "Overføring Innland, Eksempel Holding As"}
             for i, (d, a) in enumerate([(date(2025, 1, 10), 1363130), (date(2025, 2, 13), 282846),
                                         (date(2025, 4, 7), 42709716), (date(2025, 5, 5), 220000)])]
    other = {"id": 950, "booking_date": date(2025, 1, 10), "amount_minor": 1363130,
             "description": "Lønn"}
    out = lent([out_, equity, early, other, *backs],
               [{"txn_id": 900, "repaid_by": r"eksempel holding"}])
    assert out[900] == ("lending", None, "lending:out", -43000000, 0)
    assert out[903] == ("lending", 900, "lending:repaid", 1363130, 0)
    assert out[904][3:] == (282846, 0)
    # the payment that clears the loan: the rest of the principal, then interest
    assert out[905][3:] == (43000000 - 1363130 - 282846, 42709716 - (43000000 - 1363130 - 282846))
    assert 906 not in out and 901 not in out and 902 not in out and 950 not in out
    assert lent([out_], [{"txn_id": 999, "repaid_by": "x"}]) == {}       # unknown payment
    # with the day of the last payment, the May payment is interest on the loan
    closed = lent([out_, *backs, {**other, "id": 951, "booking_date": date(2025, 6, 1),
                                  "description": "Overføring Innland, Eksempel Holding As"}],
                  [{"txn_id": 900, "repaid_by": "eksempel holding", "until": date(2025, 5, 5)}])
    assert closed[906] == ("lending", 900, "lending:repaid", 0, 220000)
    assert closed[905] == out[905] and 951 not in closed


def test_transfer_to_own_name_is_an_own_move():
    paid_back = tx(1, 1, -50000, "Overføring Innland, Ola Eksempel Nordmann, Nettgiro")
    to_own = tx(1, 5, -1500, "Overføring Innland, Ola Eksempel Nordmann, Nettgiro")
    landed = tx(2, 6, 1500, OWN)
    someone = tx(1, 7, -4000, "Overføring Innland, Kari Annen, Nettgiro")
    out = classify([paid_back, to_own, landed, someone], ACCOUNTS)
    # the other side unseen: into the rammelån
    assert out[paid_back["id"]] == ("loan", None, "loan:credit_line_transfer")
    assert out[to_own["id"]] == ("own_transfer", landed["id"], "own:paired")
    assert someone["id"] not in out


def test_funds_bought_and_sold_are_saving():
    standing = tx(1, 2, -200, "Giro, Dnb Global Indeks A, Fast Oppdrag")
    sold = tx(1, 3, 45663, "Dnb Global Indeks A Overføring Innland, Dnb Global Indeks A")
    shares = tx(1, 4, 25145, "Verdipapir, Dnb Aksjehandel, Pb0000000001")
    buyback = tx(1, 5, 33010, "tilbakekjøp 10.000 Eksempel-aksjer Overføring Innland, Eksempel AS")
    dividend = tx(1, 6, 2408, "Utbytte, Ark.Ref *1 Dato 19.10, Utb No0000000000 Te")
    to_ask = tx(1, 7, -2000, "Kontoregulering, Spareavtale Til Aksjesparekonto")
    on_ask = tx(2, 7, 2000, "Kontoregulering, Spareavtale Til Aksjesparekonto")
    out = classify([standing, sold, shares, buyback, dividend, to_ask, on_ask], ACCOUNTS)
    assert out[standing["id"]] == ("saving", None, "saving:fund")
    for t in (sold, shares, buyback):
        assert out[t["id"]] == ("saving", None, "saving:sold")
    assert dividend["id"] not in out
    assert out[to_ask["id"]] == ("own_transfer", on_ask["id"], "own:paired")
