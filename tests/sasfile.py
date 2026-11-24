"""A synthetic SAS EuroBonus Mastercard export, laid out as the real one
(docs/formats/sas-mastercard.md). No real values."""

import io
from datetime import datetime

import openpyxl

HEADER = ["Dato", "Bokført", "Spesifikasjon", "Sted", "Valuta", "Utl. beløp", "Beløp", None]


def d(text):
    return datetime.strptime(text, "%Y-%m-%d")


# (purchase date, booked, text, place, currency, foreign amount, NOK, rate or None)
EVENTS = [
    ("2025-08-20", "2025-08-20", "Innbetaling", "", "NOK", 0, -1500.00, None),
    ("2025-09-20", "2025-09-20", "Innbetaling", "", "NOK", 0, -820.57, None),
    ("2025-09-30", "2025-10-01", "Kortkontingent", "", "NOK", 0, 2335, None),
]
OLA = [
    ("2025-08-02", "2025-08-04", "TESTKAFE AS", "OSLO", "NOK", 0, 1500, None),
    ("2025-09-05", "2025-09-05", "PADDLE.NET* EKSEMPEL", "LISBOA", "USD", 1.99, 20.57,
     "Valutakurs: 10.336683 Valutapåslag inngår med 2,00\xa0%"),
    ("2025-10-10", "2025-10-12", "PRØVEBUTIKK", "BERGEN", "NOK", 0, 799, None),
]
KARI = [
    ("2025-09-12", "2025-09-15", "TOKYO EKSEMPEL", "TOKYO", "JPY", 12720, 800,
     "Valutakurs: 0.062893 Valutapåslag inngår med 2,00\xa0%"),
    ("2025-10-20", "2025-10-22", "SAS0000000000001", "STOCKHOLM-ARL", "NOK", 0, -819, None),
]


def build(events=EVENTS, cards=(("540185******0001", "Ola Eksempel", OLA),
                                 ("540185******0002", "Kari Eksempel", KARI)),
          break_total=False) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Transaksjoner"
    ws.append(["Transaksjonseksport", "", "", "", "", "", "16.11.2026, 21:04:19", ""])
    ws.append([""] * 8)
    ws.append(["Totalt andre hendelser"] + [""] * 7)
    ws.append(HEADER)

    def rows(items):
        total = 0
        for dato, booked, text, place, cur, foreign, nok, rate in items:
            ws.append([d(dato), d(booked), text, place, cur, foreign, nok, None])
            if rate:
                ws.append([rate, "", "", "", "", "", "", ""])
            total += nok
        return total

    cards_total = sum(sum(i[6] for i in items) for _, _, items in cards)
    total = rows(events)
    ws.append(["", "", "Saldo hendelser", "", "", "", cards_total, ""])
    ws.append(["Totalbeløp", "", "", "", "", "", total + cards_total + (1 if break_total else 0), ""])
    ws.append([""] * 8)
    for number, holder, items in cards:
        ws.append([number, holder, None, None, None, None, None, None])
        ws.append(["Kjøp/uttak"] + [None] * 7)
        ws.append(HEADER)
        section_total = rows(items)
        ws.append(["Totalbeløp", "", "", "", "", "", section_total, ""])
        ws.append([""] * 8)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
