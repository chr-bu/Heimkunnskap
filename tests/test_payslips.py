from datetime import date

import pytest

from heimkunnskap import payslips
from tests import payslip_text


def test_dfo_slip():
    slip = payslips.parse_text(payslip_text.dfo(extra=1000000))
    assert slip.layout == payslips.DFO
    assert (slip.employer, slip.employer_orgnr) == ("EKSEMPELETATEN", "999999999")
    assert slip.person_name == "Ola Eksempel Nordmann"         # "Nordmann, Ola Eksempel"
    assert (slip.period, slip.pay_date) == (date(2026, 11, 1), date(2026, 11, 12))
    assert (slip.gross_ytd, slip.tax_base_ytd, slip.withheld_ytd) == (55000000, 48000000, 16000000)
    assert (slip.salary, slip.benefits, slip.pension) == (5000000, 51600, 100000)
    assert (slip.withholding, slip.withheld_period) == (1500000, 1560000)
    assert (slip.tax_table, slip.tax_percent, slip.pension_ytd) == ("8888", 48.0, 1100000)
    assert slip.net_paid == 5000000 + 1000000 - 100000 - 1500000 - 60000
    assert [l.code for l in slip.lines][:2] == ["1100", "1100S"]


def test_layout_b_slip():
    slip = payslips.parse_text(payslip_text.layout_b())
    assert slip.layout == payslips.LAYOUT_B
    assert (slip.employer, slip.employer_orgnr) == ("Eksempel AS", "999999999")
    assert slip.person_name == "Kari Eksempel Nordmann"
    assert (slip.period, slip.pay_date) == (date(2026, 10, 1), date(2026, 10, 20))
    # holiday pay is outside the withholding base but taxed all the same
    assert (slip.gross_ytd, slip.tax_base_ytd) == (57000000, 50000000)
    assert (slip.withheld_ytd, slip.withholding, slip.withheld_period) == (18000000, 2000000, 2000000)
    assert (slip.salary, slip.benefits, slip.pension) == (6000000, 91400, 0)
    assert (slip.tax_table, slip.tax_percent) == ("7777", 47.0)
    assert slip.net_paid == 6000000 - 2000000 - 45000


def test_not_a_payslip():
    with pytest.raises(payslips.PayslipError):
        payslips.parse_text("Faktura 1-26\nTotalt kr. 40000")
    with pytest.raises(payslips.PayslipError):
        payslips.parse(b"%PDF-1.4 broken")


def test_name_key_ignores_order():
    assert payslips.name_key("Nordmann, Ola Eksempel") == payslips.name_key("Ola Eksempel Nordmann")


def test_why_a_pdf_cannot_be_read():
    import io
    import pypdf
    from heimkunnskap.fileimport import pdf_problem

    def pdf(user=None, owner=None):
        w = pypdf.PdfWriter()
        w.add_blank_page(100, 100)
        if user is not None:
            w.encrypt(user, owner_password=owner, algorithm="AES-128")
        b = io.BytesIO()
        w.write(b)
        return b.getvalue()
    assert pdf_problem(pdf()) is None
    assert pdf_problem(pdf("", owner="x")) is None                 # only copying limited: readable
    assert "password-protected" in pdf_problem(pdf("010190"))
    assert "web page" in pdf_problem(b"<!DOCTYPE html><html><body>Logg inn</body></html>")
    assert "empty" in pdf_problem(b"")
    assert "damaged" in pdf_problem(pdf()[:60])
