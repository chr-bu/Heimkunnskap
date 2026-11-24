from datetime import date

import pytest

from heimkunnskap import claims
from heimkunnskap.claims import ClaimLine, Txn
from tests import claim_text


def test_a_claim_abroad():
    c = claims.parse_text(claim_text.abroad())
    assert (c.claim_no, c.person_name, c.purpose) == ("100001", "Ola Eksempel Nordmann",
                                                      "Deltakelse Eksempelkonferansen")
    assert (c.trip_from, c.trip_to) == (date(2026, 5, 4), date(2026, 5, 6))
    assert (c.total, c.tax_free, c.taxable) == (1580000, 1490000, 90000)
    kinds = [(l.section, l.kind, l.amount) for l in c.lines]
    assert kinds == [("allowance", "per_diem", 180000), ("allowance", "deduction", -20000),
                     ("cost", "hotel", 1000000), ("cost", "flight", 250000),
                     ("cost", "taxi", 50000), ("cost", "train", 20000),
                     ("cost", "allowance", 100000)]
    hotel = c.lines[2]
    # a note that starts like a cost type ("Fratrekk ...") is still a note
    assert hotel.text == "Hotellregning utland" and hotel.note.startswith("Fratrekk på 10 Euro")
    assert (hotel.currency, hotel.currency_amount) == ("EUR", 100000)
    assert c.outlays == 1000000 + 250000 + 50000 + 20000
    assert "99999" not in repr(c) and "Eksempelveien" not in repr(c)


def test_a_scanned_claim_with_tolls_under_the_mileage():
    c = claims.parse_text(claim_text.domestic_ocr(), ocr=True)
    assert [(l.kind, l.amount) for l in c.lines if l.section == "cost"] == [
        ("mileage", 53000), ("toll", 2000), ("toll", 0), ("parking", 15000)]
    toll = c.lines[3]
    assert toll.day == date(2026, 3, 10) and toll.note == "Bomstasjon Nord"
    assert all("->" not in l.note for l in c.lines)          # no routes, no addresses
    assert c.person_name == "Ola Eksempel Nordmann" and c.ocr


def test_a_misread_claim_is_refused():
    with pytest.raises(claims.ClaimError, match="add up"):
        claims.parse_text(claim_text.abroad(hotel="10001,00"))
    with pytest.raises(claims.ClaimError, match="tax-free and taxable"):
        claims.parse_text(claim_text.abroad(taxable="901,00"))
    with pytest.raises(claims.ClaimError, match="not an expense claim"):
        claims.parse_text("LØNNSSPESIFIKASJON\nForskuddstrekk")


def test_the_payout_is_the_total_less_tax_on_the_taxable_part():
    # 48% of 1,889.48 is 906.95: whole kroner withheld, so 906
    assert claims.expected_payouts(2033149, 188948, [48.0]) == [2033149, 1942549]
    assert claims.expected_payouts(100000, 0, [0]) == [100000]


def line(kind, day, amount, currency=None):
    return ClaimLine("cost", kind, day, "", "", amount, currency)


def test_outlays_find_their_card_payments():
    d = date(2026, 5, 6)
    txns = [Txn(1, date(2026, 5, 7), -1024600, "HOTEL EKSEMPEL PARIS"),      # extras on the bill
            Txn(2, date(2026, 3, 1), -250000, "Scandinavian Airline BE Oslo"),  # booked early
            Txn(3, date(2026, 5, 4), -49100, "SumUp *TAXIS PARIS"),          # another rate
            Txn(4, date(2026, 5, 6), -20000, "Vipps*FLYTOGET AS"),
            Txn(5, date(2026, 5, 6), -20000, "Vipps*FLYTOGET AS"),           # a second ticket
            Txn(9, date(2026, 5, 12), -20000, "Vipps*FLYTOGET AS"),          # a later trip
            Txn(6, date(2026, 5, 5), -50000, "RESTAURANT EKSEMPEL"),         # not a taxi
            Txn(7, date(2026, 5, 8), -60000, "UBER TRIP"),
            Txn(8, date(2026, 5, 9), -60030, "UBER TRIP")]
    lines = [(10, line("hotel", d, 1000000, "EUR")), (11, line("flight", date(2026, 5, 4), 250000)),
             (12, line("taxi", date(2026, 5, 4), 50000, "EUR")), (13, line("train", d, 20000)),
             (14, line("taxi", date(2026, 5, 9), 60020, "EUR")), (15, line("parking", d, 15000))]
    found = claims.match_lines(lines, txns, set())
    assert found[10] == (1, False) and found[11] == (2, False) and found[12] == (3, False)
    assert found[13] == (4, False)                  # two equal tickets: either, not a guess
    assert found[14] == (8, True)                   # closest amount, but 7 fits too
    assert found[15] == (None, False)


def test_a_payment_is_used_once_and_exact_amounts_go_first():
    d = date(2026, 5, 6)
    txns = [Txn(1, d, -50000, "TAXI A"), Txn(2, d, -50800, "TAXI B")]
    lines = [(1, line("taxi", d, 50500, "EUR")), (2, line("taxi", d, 50000, "EUR"))]
    found = claims.match_lines(lines, txns, set())
    assert found[2][0] == 1 and found[1][0] == 2


def test_an_outlay_claim_without_a_trip():
    c = claims.parse_text(claim_text.outlay())
    assert (c.claim_no, c.title, c.purpose) == ("100003", "Utlegg - innland", "Abonnement fagblad")
    assert (c.trip_from, c.trip_to, c.total, c.taxable) == (date(2026, 3, 17), date(2026, 3, 17),
                                                           150000, 0)
    [l] = c.lines
    assert (l.kind, l.outlay, l.amount) == ("subscription", True, 150000)
    # matched by the exact amount only
    txns = [Txn(1, date(2026, 3, 16), -150000, "FAGBLAD AS"), Txn(2, date(2026, 3, 16), -150100, "X")]
    assert claims.match_lines([(1, l)], txns, set()) == {1: (1, False)}
    assert claims.match_lines([(1, l)], txns[1:], set()) == {1: (None, False)}
