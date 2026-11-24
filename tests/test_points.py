import re
from datetime import date

import pytest

from heimkunnskap import eurobonus, points
from heimkunnskap.points import Rule
from tests import eurobonus_file


def test_the_eurobonus_export():
    content = eurobonus_file.export()
    assert eurobonus.detect(content) and not eurobonus.detect(b"PK\x03\x04 not excel")
    rows = eurobonus.parse(content)
    assert len(rows) == 10
    assert (rows[0].partner, rows[0].kind, rows[0].bonus_points) == ("DNB Rewards", "Extra Points", 1650)
    assert (rows[5].partner, rows[5].bonus_points) == ("Oslo - Bangkok", -190000)
    assert (rows[8].partner, rows[8].kind) == ("EuroBonus Online Shopping, Books", "Extra Points")
    assert rows[3].fingerprint != rows[4].fingerprint                  # two equal rows: both kept
    assert rows[3].fingerprint == eurobonus.parse(content)[3].fingerprint   # and stable
    with pytest.raises(eurobonus.EuroBonusError):
        eurobonus.parse(eurobonus_file.export(rows=[("not a date", "x", 1, None)]))


def test_merchant_key():
    assert points.merchant_key(None, "AER*KIWI 100 EKSEMPELVEI, OSLO") == "kiwi"
    assert points.merchant_key(None, "KIWI 100 EKSEMPEL") == "kiwi"
    assert points.merchant_key(None, "LE PROCOPE PARIS") == "le procope"
    assert points.merchant_key("Vipps*Ruter", "Vipps*Ruter, Oslo") == "ruter"


RULES = [Rule("amex", date(2017, 1, 1), None, 20, 20, None, 3),
         Rule("dnb", date(2017, 1, 1), None, 15, 20, 1_000_000, 7),     # a 10,000 kr cap, for the test
         Rule("sas", date(2017, 1, 1), None, 15, 15, None, 31)]
EXCLUDE = [(re.compile(r"^vipps\*(?!.*\b(as|ruter)\b)[a-zæøå]+ [a-zæøå]+"), "Vipps to a person")]


def txn(i, account, day, kr, text, currency=None):
    return {"id": i, "account_id": account, "booking_date": day, "amount_minor": round(kr * 100),
            "original_currency": currency, "counterparty": None, "description": text}


def test_expected_points_and_the_best_card():
    card_of = {1: "amex", 2: "dnb", 3: "sas"}
    txns = [txn(1, 1, date(2026, 3, 2), -100, "KIWI 100 EKSEMPEL"),
            txn(2, 2, date(2026, 3, 5), -200, "AER*KIWI 100 EKSEMPELPO"),       # Amex known there
            txn(3, 2, date(2026, 3, 6), -300, "BAKERI EKSEMPEL"),             # Amex not known there
            txn(4, 2, date(2026, 3, 26), -1000, "HOTEL EKSEMPEL PARIS", "EUR"),  # next period, foreign
            txn(5, 2, date(2026, 3, 27), -500, "Vipps*Ola Nordmann, Oslo"),     # a transfer
            txn(6, 2, date(2026, 3, 28), 100, "AER*KIWI 100 EKSEMPELPO"),       # a refund
            txn(7, 2, date(2026, 4, 1), -9000, "BAKERI EKSEMPEL"),             # reaches the cap
            txn(8, 2, date(2026, 4, 2), -50, "BAKERI EKSEMPEL"),               # over it
            txn(9, 3, date(2026, 4, 2), -100, "BAKERI EKSEMPEL"),
            dict(txn(10, 3, date(2026, 4, 3), -10, "Vipps*Tabs, Oslo"), counterparty="Vipps*Tabs")]
    rows = {r[0]: r for r in points.compute(txns, card_of, RULES, EXCLUDE, {"amex"})}
    # (id, card, period, earning, foreign, points, excluded, best card, best points, merchant)
    assert rows[1][5:9] == (20.0, None, "amex", 20.0)
    assert rows[2][5:9] == (30.0, None, "amex", 40.0)              # 10 points missed
    assert rows[3][5:9] == (45.0, None, "dnb", 45.0)               # Amex not known: nothing missed
    assert (rows[4][2], rows[4][4], rows[4][5]) == (date(2026, 4, 1), True, 200.0)
    assert rows[5][3:9] == (0, False, 0.0, "Vipps to a person", "dnb", 0.0)
    assert rows[6][5] == -15.0                                    # a refund takes points back
    assert rows[7][3] == -850000 and rows[7][5] == 1275.0          # 8,500 kr left under the cap
    assert rows[8][3:7] == (0, False, 0.0, "over the yearly cap")
    assert rows[9][1:3] == ("sas", date(2026, 5, 1))               # posted the month after
    assert rows[10][6] is None              # a shop through Vipps: the payee repeating is no name


def test_award_bookings_and_their_refunds():
    rows = [{"id": i, "day": date.fromisoformat(d), "partner": p, "bonus_points": b, "level_points": 0}
            for i, (d, p, b) in enumerate([
                ("2025-12-11", "Oslo - Singapore", -95000),
                ("2025-12-31", "Oslo - Singapore", -95000),
                ("2026-02-16", "Oslo - Paris", -60000),
                ("2026-03-01", "Oslo - Paris", -30000),               # kept
                ("2026-05-27", "Oslo - Tokyo", -120000),
                ("2026-05-27", "Scandinavian Airlines System", 120000),  # same day: changed
                ("2026-05-27", "Scandinavian Airlines System", 95000),
                ("2026-07-16", "Scandinavian Airlines System", 60000),
                ("2026-08-01", "Scandinavian Airlines System", 5000),    # no booking of 5,000: earned
                ("2026-11-01", "Rebooking premium economy to business", -23750),
                ("2026-11-09", "Scandinavian Airlines System", 23750),
                ("2026-02-04", "EB Checkout", -187),
                ("2026-11-08", "Level Points from DNB Rewards", 0)], 1)]
    f = points.pair_awards(rows)
    assert f[2] == ("award_changed", None) and f[7] == ("award_refund", 2)   # the latest 95,000
    assert f[1] == ("award", None)                                           # still kept
    assert f[3] == ("award_changed", None) and f[8] == ("award_refund", 3)
    assert f[4] == ("award", None)
    assert f[5] == ("award_changed", None) and f[6] == ("award_refund", 5)
    assert f[9] == ("earned", None)
    assert f[10] == ("award_changed", None) and f[11] == ("award_refund", 10)
    assert f[12] == ("shop", None) and f[13] == ("level", None)
