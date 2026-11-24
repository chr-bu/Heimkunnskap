"""Card file upload and transfer pairing end to end, on PostgreSQL via psql.
Needs HEIM_TEST_PG (see test_sync_pg)."""

import asyncio
from datetime import date
import re
from pathlib import Path

import pytest

from heimkunnskap import fileimport, pairing
from tests.asgi import Client, multipart
from tests.pgshim import PsqlConn
from tests.test_web_pg import PG, bankid, web  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(not PG, reason="HEIM_TEST_PG not set")
FIX = Path(__file__).parent / "fixtures"
AMEX = (FIX / "amex_sample.xlsx").read_bytes()
DNB = (FIX / "dnb_mastercard_sample.xlsx").read_bytes()


def run(coro):
    return asyncio.run(coro)


async def upload(c, files, csrf=None):
    if csrf is None:
        page = await c.get("/upload")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    body, ctype = multipart({"csrf": csrf}, files)
    return await c.post("/upload", body=body, headers={"content-type": ctype})


def test_upload_page_imports_skips_and_rejects(web):
    app, api, _ = web

    async def go():
        c = Client(app, client_ip="192.168.1.20")
        first = await upload(c, [("files", "aktivitet.xlsx", AMEX),
                                 ("files", "transaksjonsliste_1.xlsx", DNB),
                                 ("files", "notes.xlsx", b"not a spreadsheet")])
        again = await upload(c, [("files", "aktivitet (1).xlsx", AMEX)])
        forged = await upload(c, [("files", "a.xlsx", AMEX)], csrf="nope")
        return first, again, forged
    first, again, forged = run(go())
    assert first.status == 200
    assert "American Express, " in first.text and "5 rows, 5 new" in first.text
    assert "DNB Mastercard, 03.11.2026–15.11.2026: 10 rows, 10 new" in first.text
    assert "Not imported: not an Excel file" in first.text
    assert "Already uploaded" in again.text
    assert forged.status == 303                               # form expired, nothing read

    conn = PsqlConn(PG)
    cards = run(conn.fetch("""SELECT account_key, cash_account_type, count(t.id) AS n
                              FROM account a LEFT JOIN txn t ON t.account_id = a.id
                              WHERE source = 'file' GROUP BY 1, 2 ORDER BY 1"""))
    assert cards == [{"account_key": "file:amex:12345", "cash_account_type": "CARD", "n": 5},
                     {"account_key": "file:dnb_mastercard", "cash_account_type": "CARD", "n": 10}]
    holders = run(conn.fetch("SELECT DISTINCT card_holder FROM txn WHERE card_holder IS NOT NULL "
                             "ORDER BY 1"))
    assert [h["card_holder"] for h in holders] == ["KARI EKSEMPEL NORDMANN", "OLA EKSEMPEL NORDMANN"]
    raw = run(conn.fetchval("SELECT count(*) FROM raw_record WHERE kind = 'file'"))
    assert raw == 3                                            # the rejected file is kept too
    leaked = run(conn.fetchval("SELECT count(*) FROM txn WHERE extra::text LIKE '%Billett%' "
                               "OR extra::text LIKE '%NORDMANN/KARI%'"))
    assert leaked == 0


def test_grown_period_adds_only_new_rows(web):
    import io
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(DNB))
    wb["transaksjonsliste"].append([None, "NY KAFE, OSLO", None, None, None, 55])
    wb["transaksjonsliste"]["A13"] = wb["transaksjonsliste"]["A12"].value
    grown = io.BytesIO()
    wb.save(grown)
    conn = PsqlConn(PG)
    first = run(fileimport.import_file(conn, "p1.xlsx", DNB))
    second = run(fileimport.import_file(conn, "p1-grown.xlsx", grown.getvalue()))
    assert (first["rows"], first["added"]) == (10, 10)
    assert (second["rows"], second["added"]) == (11, 1)


def test_card_bill_pairs_with_the_uploaded_card(web):
    app, api, _ = web
    run(bankid(app, api))                       # DNB accounts and their transactions
    conn = PsqlConn(PG)
    dnb_account = run(conn.fetchval("SELECT min(id) FROM account WHERE source = 'enable_banking'"))
    # The Amex bill as it appears in the DNB feed, a day before Amex books it
    run(conn.execute(
        """INSERT INTO txn (account_id, booking_date, amount_minor, currency, description,
                            fingerprint, batch_id)
           VALUES ($1, '2026-10-31', -250000, 'NOK',
                   '100000000000001 Giro, American Express, Efaktura', 'test-amex-bill',
                   (SELECT min(id) FROM import_batch))""", dnb_account))
    run(pairing.run(conn))
    before = run(conn.fetch("SELECT kind, rule FROM report.review"))
    assert {"kind": "card_payment", "rule": "card:amex"} in before
    # with no card data yet, the bill stands in for its purchases
    oct_before = run(conn.fetchrow("SELECT spending, unmatched_moves, card_bills_no_detail "
                                   "FROM report.household_monthly WHERE month = '2026-10-01'"))
    assert oct_before["unmatched_moves"] == 0
    assert oct_before["card_bills_no_detail"] == 2500 and oct_before["spending"] >= 2500
    assert run(conn.fetchval("SELECT category FROM report.transactions "
                             "WHERE description LIKE '%Giro, American Express%'")) == \
        "Card purchases (no detail)"

    result = run(fileimport.import_file(conn, "aktivitet.xlsx", AMEX))
    assert result["status"] == "imported"
    pair = run(conn.fetch(
        """SELECT t.description, c.kind, c.rule FROM txn_class c JOIN txn t ON t.id = c.txn_id
           WHERE c.rule = 'card:amex' AND c.partner_txn_id IS NOT NULL ORDER BY t.amount_minor"""))
    assert [p["description"] for p in pair] == [
        "100000000000001 Giro, American Express, Efaktura", "BETALING MOTTATT - TAKK"]
    after = run(conn.fetch("SELECT rule FROM report.review"))
    assert {"rule": "card:amex"} not in after
    assert run(conn.fetchval("SELECT category FROM report.transactions "
                             "WHERE description LIKE '%Giro, American Express%'")) is None

    # The household view: the payment is neither spending nor income;
    # the card purchases are spending.
    nov = run(conn.fetchrow("SELECT * FROM report.household_monthly WHERE month = '2026-11-01'"))
    oct_ = run(conn.fetchrow("SELECT * FROM report.household_monthly WHERE month = '2026-10-01'"))
    assert nov["spending"] >= 499 + 312.75 + 1990          # the November Amex charges
    assert oct_["unmatched_moves"] == 0
    card_side = run(conn.fetchrow(
        "SELECT money_in, transfers FROM report.monthly m JOIN account a ON a.id = m.account_id "
        "WHERE a.account_key = 'file:amex:12345' AND month = '2026-11-01'"))
    assert card_side == {"money_in": 0, "transfers": 2500}


def test_reminder_when_a_card_file_is_overdue(web, monkeypatch):
    from datetime import date
    from heimkunnskap import notify, sync
    sent = []
    monkeypatch.setattr(notify, "send", lambda title, body="", priority=0: sent.append((title, body)))
    conn = PsqlConn(PG)
    run(fileimport.import_file(conn, "p1.xlsx", DNB))              # newest 15.11.2026
    run(sync.check_card_files(conn, today=date(2026, 12, 20)))     # 35 days: not yet
    run(sync.check_card_files(conn, today=date(2026, 12, 27)))     # 42 days: due
    run(sync.check_card_files(conn, today=date(2026, 12, 28)))     # reminded already
    run(sync.check_card_files(conn, today=date(2027, 1, 3)))       # a week later
    assert [t for t, _ in sent] == ["DNB Mastercard: time for a new card file"] * 2
    assert "15.11.2026" in sent[0][1] and "/upload" in sent[0][1]


def test_loans_and_interest_in_the_household_view(web):
    conn = PsqlConn(PG)
    acct = run(conn.fetchval("INSERT INTO account (account_key, source, aspsp_name, cash_account_type) "
                             "VALUES ('h-main', 'enable_banking', 'DNB', 'CACC') RETURNING id"))
    batch = run(conn.fetchval("INSERT INTO import_batch (source, kind) VALUES ('t', 't') RETURNING id"))
    for i, (amount, text) in enumerate([
            (-1000000, "Lån, Lån 1111.22.33333, Avdrag Kr 6.000,00, Renter Kr 3.935,00, Omk. Kr 65,00"),
            (-2156500, "Lån, Lån 4444.55.66666, Avdrag Kr 0,00, Renter Kr 21.500,00, Omk. Kr 65,00"),
            (3050000, "Kontoregulering, Overføring Mellom Egne Konti"),      # drawn from the rammelån
            (-5300000, "Overføring Innland, Rammelån, Nettgiro")]):           # paid back
        run(conn.execute(
            "INSERT INTO txn (account_id, booking_date, amount_minor, currency, description, "
            "fingerprint, batch_id) VALUES ($1, '2026-10-20', $2, 'NOK', $3, $4, $5)",
            acct, amount, text, f"fp{i}", batch))
    run(pairing.run(conn))
    month = run(conn.fetchrow("SELECT loans, interest, spending, unmatched_moves "
                              "FROM report.household_monthly WHERE month = '2026-10-01'"))
    # principal: 6,000 repaid, 30,500 drawn, 53,000 repaid; interest and fees 4,000 + 21,565
    assert month == {"loans": 28500, "interest": 25565, "spending": 0, "unmatched_moves": 0}
    split = run(conn.fetchrow("SELECT loan_principal, loan_interest, loan_fees FROM "
                              "report.transactions WHERE description LIKE 'Lån, Lån 1111%'"))
    assert split == {"loan_principal": -6000, "loan_interest": -3935, "loan_fees": -65}


def test_reimbursement_reduces_spending(web):
    conn = PsqlConn(PG)
    acct = run(conn.fetchval("INSERT INTO account (account_key, source, aspsp_name, cash_account_type) "
                             "VALUES ('h-r', 'enable_banking', 'DNB', 'CACC') RETURNING id"))
    batch = run(conn.fetchval("INSERT INTO import_batch (source, kind) VALUES ('t', 't') RETURNING id"))
    for i, (day, amount, text) in enumerate([
            ("2026-03-01", -1299000, "POWER.no / 100000001"),
            ("2026-03-03", 1299000, "utlegg ABCD Overføring Innland, Eksempel Consulting AS"),
            ("2026-03-05", 4000000, "20 timer Overføring Innland, Eksempel Consulting AS")]):
        run(conn.execute(
            "INSERT INTO txn (account_id, booking_date, amount_minor, currency, description, "
            "fingerprint, batch_id) VALUES ($1, $2, $3, 'NOK', $4, $5, $6)",
            acct, day, amount, text, f"r{i}", batch))
    run(pairing.run(conn))
    march = run(conn.fetchrow("SELECT income, spending, net FROM report.household_monthly "
                              "WHERE month = '2026-03-01'"))
    assert march == {"income": 40000, "spending": 0, "net": 40000}


def test_categories_page_and_rules(web):
    app, api, _ = web
    conn = PsqlConn(PG)
    assert run(conn.fetchval("SELECT count(*) FROM category_rule")) >= 15     # seeded

    async def go():
        c = Client(app, client_ip="192.168.1.20")
        await upload(c, [("files", "aktivitet.xlsx", AMEX), ("files", "dnb.xlsx", DNB)])
        page = await c.get("/categories?year=2026")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        form = {"content-type": "application/x-www-form-urlencoded"}
        bad = await c.post("/categories/rules", headers=form,
                           body=f"csrf={csrf}&pattern=(oops&category=X&priority=5")
        after_bad = await c.get("/categories?year=2026")
        good = await c.post("/categories/rules", headers=form,
                            body=f"csrf={csrf}&pattern=testkafe&category=Eating+out&priority=5")
        after = await c.get("/categories?year=2026")
        return page, bad, after_bad, good, after
    page, bad, after_bad, good, after = run(go())
    assert page.status == 200 and "Largest payees in Other" in page.text
    assert "TESTKAFE AS" in page.text or "Other" in page.text
    assert bad.status == 303 and "Not saved: not a valid pattern" in after_bad.text
    assert good.status == 303 and "Rule saved for Eating out" in after.text
    cat = run(conn.fetchval("SELECT category FROM report.transactions "
                            "WHERE description LIKE 'TESTKAFE%' LIMIT 1"))
    assert cat == "Eating out"
    # the payment from the account is not spending, so it has no category
    assert run(conn.fetchval("SELECT category FROM report.transactions "
                             "WHERE description = 'BETALING MOTTATT - TAKK'")) is None
    monthly = run(conn.fetch("SELECT category FROM report.category_monthly"))
    assert {"category": "Eating out"} in monthly


def test_interest_netted_out_of_a_rammelan_payment(web):
    """The bank takes the month's interest out of a direct payment to the credit line;
    the text says nothing of it. `manual check` finds the month, `manual split` fixes it."""
    from heimkunnskap import manual
    conn = PsqlConn(PG)
    acct = run(conn.fetchval("INSERT INTO account (account_key, source, aspsp_name, cash_account_type) "
                             "VALUES ('h-rl', 'enable_banking', 'DNB', 'CACC') RETURNING id"))
    batch = run(conn.fetchval("INSERT INTO import_batch (source, kind) VALUES ('t', 't') RETURNING id"))
    ids = {}
    for i, (key, day, amount, text) in enumerate([
            ("apr", "2026-04-20", -2631185, "Lån, Lån 4444.55.66666, Avdrag Kr 0,00, "
                                             "Renter Kr 26.246,85, Omk. Kr 65,00"),
            ("apr_pay", "2026-04-27", -3000000, "Overføring Innland, Rammelån, Nettgiro"),
            ("may_a", "2026-05-18", -1500000, "Overføring Innland, Rammelån, Nettgiro"),
            ("may_b", "2026-05-18", -4000000, "Overføring Innland, Rammelån, Nettgiro")]):
        ids[key] = run(conn.fetchval(
            "INSERT INTO txn (account_id, booking_date, amount_minor, currency, description, "
            "fingerprint, batch_id) VALUES ($1, $2, $3, 'NOK', $4, $5, $6) RETURNING id",
            acct, day, amount, text, f"rl{i}", batch))
    run(pairing.run(conn))
    may = "SELECT loans, interest FROM report.household_monthly WHERE month = '2026-05-01'"
    assert run(conn.fetchrow(may)) == {"loans": 55000, "interest": 0}      # all taken for principal
    gaps = run(manual.credit_line_gaps(conn, date(2026, 11, 17)))
    assert [(g["month"], g["payments"], float(g["total"])) for g in gaps] == [(date(2026, 5, 1), 2, 55000.0)]
    assert "2026-05-01" in run(manual.check(conn))
    assert "1 month(s)" in run(manual.listing(conn))

    with pytest.raises(SystemExit):                                     # must add up
        run(manual.split_loan(conn, ids["may_b"], 29598.65, 10401.00, 0))
    with pytest.raises(SystemExit):
        run(manual.split_loan(conn, ids["may_b"], 29598.65, -10401.35 + 20802.70, -10401.35))
    run(manual.split_loan(conn, ids["may_b"], 29598.65, 10401.35, 0))
    out = run(manual.split_loan(conn, ids["may_a"], 0, 14935, 65))
    assert "interest 14 935.00" in out
    assert run(conn.fetchrow(may)) == {"loans": 29598.65, "interest": 25401.35}
    row = run(conn.fetchrow("SELECT rule, loan_principal, loan_interest, loan_fees FROM "
                            "report.transactions WHERE id = $1", ids["may_a"]))
    assert row == {"rule": "manual:loan_split", "loan_principal": 0, "loan_interest": -14935,
                   "loan_fees": -65}
    assert run(manual.credit_line_gaps(conn, date(2026, 11, 17))) == []
    run(pairing.run(conn))                                              # a rerun keeps the split
    assert run(conn.fetchrow(may)) == {"loans": 29598.65, "interest": 25401.35}
    run(manual.unmark(conn, ids["may_a"]))                              # undone: back to principal
    assert run(conn.fetchrow(may))["loans"] == 44598.65


def test_money_lent_to_an_own_company(web, capsys):
    from heimkunnskap import manual
    conn = PsqlConn(PG)
    acct = run(conn.fetchval("INSERT INTO account (account_key, source, aspsp_name, cash_account_type) "
                             "VALUES ('h-l', 'enable_banking', 'DNB', 'CACC') RETURNING id"))
    batch = run(conn.fetchval("INSERT INTO import_batch (source, kind) VALUES ('t', 't') RETURNING id"))
    ids = []
    for i, (day, amount, text) in enumerate([
            ("2024-09-13", -3000000, "Overføring Innland, Eksempel Holding As, Nettgiro"),
            ("2024-12-09", -43000000, "Overføring Innland, Eksempel Holding As, Nettgiro"),
            ("2025-01-10", 1000000, "Overføring Innland, Eksempel Holding As"),
            ("2025-04-07", 43500000, "Overføring Innland, Eksempel Holding As")]):
        ids.append(run(conn.fetchval(
            "INSERT INTO txn (account_id, booking_date, amount_minor, currency, description, "
            "fingerprint, batch_id) VALUES ($1, $2, $3, 'NOK', $4, $5, $6) RETURNING id",
            acct, day, amount, text, f"l{i}", batch)))
    equity, loan, first, last = ids
    run(pairing.run(conn))
    found = run(manual.find(conn, "eksempel holding", None, None))
    assert str(loan) in found and "430 000.00" in found.replace("-", "")

    printed = run(manual.lend(conn, loan, "Eksempel Holding", "loan to the company"))
    assert "loan to the company" in printed
    run(manual.mark(conn, equity, "saving"))
    with pytest.raises(SystemExit):
        run(manual.lend(conn, first, "x", None))                 # money in is not a loan
    with pytest.raises(SystemExit):
        run(manual.lend(conn, equity, "x", None))                # marked already

    lending = run(conn.fetchrow("SELECT lent, repaid, interest, outstanding, last_repayment "
                                "FROM report.lending"))
    assert lending == {"lent": 430000, "repaid": 430000, "interest": 15000,
                       "outstanding": 0, "last_repayment": date(2025, 4, 7)}
    months = {r["month"].isoformat(): r for r in run(conn.fetch(
        "SELECT month, income, spending, saving, net FROM report.household_monthly "
        "WHERE month < '2025-06-01'"))}
    assert months["2024-09-01"] == {"month": date(2024, 9, 1), "income": 0, "spending": 0,
                                    "saving": 30000, "net": -30000}
    assert (months["2024-12-01"]["spending"], months["2024-12-01"]["saving"]) == (0, 430000)
    assert (months["2025-01-01"]["income"], months["2025-01-01"]["saving"]) == (0, -10000)
    assert (months["2025-04-01"]["income"], months["2025-04-01"]["saving"]) == (15000, -420000)
    # a re-run keeps both: the loan in its table, the mark as manual
    run(pairing.run(conn))
    assert run(conn.fetchval("SELECT count(*) FROM txn_class WHERE kind = 'lending'")) == 3
    assert "Marked by hand" in run(manual.listing(conn))

    assert run(manual.unmark(conn, loan)) == "undone"
    assert run(manual.unmark(conn, loan)) == "nothing to undo for that transaction"
    assert run(conn.fetchval("SELECT count(*) FROM txn_class WHERE kind = 'lending'")) == 0
    assert run(conn.fetchval("SELECT spending FROM report.household_monthly "
                             "WHERE month = '2024-12-01'")) == 430000


def test_sas_file_fills_in_history_before_the_bank_feed(web):
    from tests import sasfile
    conn = PsqlConn(PG)
    batch = run(conn.fetchval("INSERT INTO import_batch (source, kind) VALUES ('t', 't') RETURNING id"))
    sas = run(conn.fetchval(
        "INSERT INTO account (account_key, source, aspsp_name, cash_account_type) "
        "VALUES ('h-sas', 'enable_banking', 'SAS EuroBonus Mastercard', 'CARD') RETURNING id"))
    main = run(conn.fetchval("INSERT INTO account (account_key, source, aspsp_name, cash_account_type) "
                             "VALUES ('h-main', 'enable_banking', 'DNB', 'CACC') RETURNING id"))
    for i, (acct, day, amount, text) in enumerate([
            (sas, "2025-10-01", -233500, "År-/mån avg"),               # the feed's own texts
            (sas, "2025-10-12", -79900, "PRØVEBUTIKK"),
            (sas, "2025-10-22", 81900, "SAS0000000000001"),
            (main, "2025-08-20", -150000, "300000000000003 Giro, Seb Kort, Efaktura")]):
        run(conn.execute(
            "INSERT INTO txn (account_id, booking_date, amount_minor, currency, description, "
            "fingerprint, batch_id) VALUES ($1, $2, $3, 'NOK', $4, $5, $6)",
            acct, day, amount, text, f"s{i}", batch))

    content = sasfile.build()
    result = run(fileimport.import_file(conn, "transactions.xlsx", content))
    assert result["status"] == "imported" and (result["rows"], result["added"]) == (8, 5)
    assert ("merged with the bank feed, which starts 01.10.2025: 3 of the 3 rows from then on "
            "are in the feed") in result["checks"]
    assert run(conn.fetchval("SELECT count(*) FROM account WHERE source = 'file'")) == 0
    assert run(conn.fetchval("SELECT count(*) FROM txn WHERE account_id = $1", sas)) == 8
    assert run(conn.fetchval("SELECT card_holder FROM txn WHERE description = 'TESTKAFE AS'")) == \
        "Ola Eksempel"
    # the old bill now pairs with the payment the file brought
    bill = run(conn.fetchrow("SELECT kind, partner_id FROM report.transactions "
                             "WHERE description LIKE '%Seb Kort%'"))
    assert bill["kind"] == "card_payment" and bill["partner_id"] is not None
    fees = run(conn.fetchval("SELECT category FROM report.transactions WHERE description = 'År-/mån avg'"))
    assert fees == "Card and bank fees"
    again = run(fileimport.import_file(conn, "transactions (1).xlsx", content))
    assert again["status"] == "duplicate"


def test_sas_file_without_a_bank_feed_is_its_own_card(web):
    from tests import sasfile
    conn = PsqlConn(PG)
    result = run(fileimport.import_file(conn, "transactions.xlsx", sasfile.build()))
    assert (result["rows"], result["added"]) == (8, 8)
    card = run(conn.fetchrow("SELECT account_key, aspsp_name, cash_account_type FROM account"))
    assert card == {"account_key": "file:sas_mastercard", "aspsp_name": "SAS EuroBonus Mastercard",
                    "cash_account_type": "CARD"}
    fee = run(conn.fetchval("SELECT category FROM report.transactions WHERE description = 'Kortkontingent'"))
    assert fee == "Card and bank fees"


def test_payslips_and_the_tax_estimate(web, monkeypatch):
    from heimkunnskap import manual, payslips, tax
    from tests import payslip_text
    conn = PsqlConn(PG)
    batch = run(conn.fetchval("INSERT INTO import_batch (source, kind) VALUES ('t', 't') RETURNING id"))
    acct = {}
    for key, name, biz in [("h-ola", "Ola Eksempel Nordmann", False), ("h-kari", "Kari Nordmann", False),
                           ("h-biz", "Ola Eksempel Nordmann", True)]:
        acct[key] = run(conn.fetchval(
            "INSERT INTO account (account_key, source, aspsp_name, cash_account_type, name, is_business_income) "
            "VALUES ($1, 'enable_banking', 'DNB', 'CACC', $2, $3) RETURNING id", key, name, biz))
    dfo = payslip_text.dfo()
    slip_b = payslip_text.layout_b()
    dfo_net, slip_b_net = payslips.parse_text(dfo).net_paid, payslips.parse_text(slip_b).net_paid
    rows = [("h-ola", "2026-11-12", dfo_net, "Lønn, Eksempeletaten"),
            ("h-kari", "2026-10-20", slip_b_net, "Overføring Innland, Eksempel AS"),
            ("h-kari", "2026-06-30", 2910546, "Overføring Innland, Eksempel Employee Invest As"),
            ("h-biz", "2026-04-09", 4000000, "Overføring Innland, Kunde AS"),       # before VAT
            ("h-biz", "2026-05-15", 5500000, "faktura 3-26 Overføring Innland, Kunde AS"),
            ("h-biz", "2026-10-01", 1250000, "Timer august Overføring Innland, Kunde AS"),
            ("h-biz", "2026-08-18", 75800, "MVA Tilgode Overføring Innland, Skatteetaten")]
    rows += [("h-ola", f"2026-{m:02d}-20", -2565000,
              "Lån, Lån 4444.55.66666, Avdrag Kr 0,00, Renter Kr 25.585,00, Omk. Kr 65,00")
             for m in range(1, 11)]
    for i, (key, day, amount, text) in enumerate(rows):
        run(conn.execute(
            "INSERT INTO txn (account_id, booking_date, amount_minor, currency, description, "
            "fingerprint, batch_id) VALUES ($1, $2, $3, 'NOK', $4, $5, $6)",
            acct[key], day, amount, text, f"tax{i}", batch))
    run(pairing.run(conn))

    texts = {b"%PDF-dfo": dfo, b"%PDF-slip-b": slip_b}
    monkeypatch.setattr(payslips, "pdf_text", lambda content: texts[content])
    r1 = run(fileimport.import_file(conn, "dfo.pdf", b"%PDF-dfo"))
    r2 = run(fileimport.import_file(conn, "slip.pdf", b"%PDF-slip-b"))
    assert r1["status"] == "imported" and r1["kind"] == "payslip" and r1["period"] == "11.2026"
    assert any("is the deposit on 12.11.2026" in c for c in r1["checks"])
    assert any("is the deposit on 20.10.2026" in c for c in r2["checks"])
    again = run(fileimport.import_file(conn, "dfo (1).pdf", b"%PDF-dfo"))
    assert again["status"] == "duplicate"
    stored = run(conn.fetchval("SELECT count(*) FROM payslip"))
    assert stored == 2
    lines = run(conn.fetchval("SELECT lines::text FROM payslip WHERE employer = 'EKSEMPELETATEN'"))
    assert "00000000000" not in lines                     # no identity or account number

    run(manual.set_value(conn, "vat_registered_from", "2026-04-19"))
    invoice = run(conn.fetchval("SELECT id FROM txn WHERE description LIKE 'faktura 3-26%'"))
    run(manual.set_vat(conn, invoice, 15000, "includes VAT billed afterwards for March"))
    result = run(tax.estimate(conn, 2026, today=date(2026, 11, 17)))
    people = {p["person"]: p for p in result["people"]}
    ola, kari = people["Ola Eksempel Nordmann"], people["Kari Eksempel Nordmann"]
    # 40,000 before registration, 55,000 less 15,000, 12,500 less 20%; the refund is not income
    assert ola["business_profit"] == pytest.approx(40000 + 40000 + 10000)
    # Ten instalments of 25,585 interest, then November and December at that average
    assert ola["interest_deduction"] == pytest.approx(25585 * 12)
    assert kari["interest_deduction"] == 0
    assert kari["dividends"] == pytest.approx(29105.46)
    # Salary: year to date plus one more month at salary and benefits (DFØ slip is November)
    assert ola["salary"] == pytest.approx(550000 + (50000 + 516))
    assert ola["withheld"] == pytest.approx(160000 + 15000 / 2)
    assert kari["salary"] == pytest.approx(570000 + 2 * (60000 + 914))
    assert result["household"]["total_tax"] == pytest.approx(ola["total_tax"] + kari["total_tax"])
    # Each spouse deducts the interest on their own loans: a share of it to the second person
    run(manual.set_value(conn, "loan_interest_person", "Ola Eksempel Nordmann"))
    run(manual.set_value(conn, "loan_interest_share", "0.6"))
    split = {p["person"]: p for p in run(tax.estimate(conn, 2026, today=date(2026, 11, 17)))["people"]}
    assert split["Ola Eksempel Nordmann"]["interest_deduction"] == pytest.approx(25585 * 12 * 0.6)
    assert split["Kari Eksempel Nordmann"]["interest_deduction"] == pytest.approx(25585 * 12 * 0.4)
    with pytest.raises(SystemExit):
        run(manual.set_value(conn, "loan_interest_share", "60"))
    run(manual.set_value(conn, "loan_interest_share", "1"))
    # 2025 has rules but no pay slips here: nothing to estimate from
    assert run(tax.estimate(conn, 2025, today=date(2026, 11, 17)))["people"] == []
    assert "No pay slips for 2025" in run(manual.tax_report(conn, 2025))
    report = run(manual.tax_report(conn, 2026))
    assert "Refund (+) or to pay (-)" in report and "0 tagged expenses" in report


def test_tax_page_and_mcp_view(web, monkeypatch):
    from heimkunnskap import payslips
    from tests import payslip_text
    app, api, _ = web
    conn = PsqlConn(PG)
    empty = run(Client(app, client_ip="192.168.1.20").get("/tax?year=2026"))
    assert empty.status == 200 and "No pay slips yet" in empty.text
    monkeypatch.setattr(payslips, "pdf_text", lambda content: payslip_text.dfo())
    run(fileimport.import_file(conn, "dfo.pdf", b"%PDF-dfo"))
    page = run(Client(app, client_ip="192.168.1.20").get("/tax?year=2026"))
    assert page.status == 200
    assert "Ola Eksempel Nordmann" in page.text and "Refund, or tax to pay" in page.text
    assert "00000000000" not in page.text
    assert "Household</th>" not in page.text                     # one person: no sum column
    stored = run(conn.fetch("SELECT person, total_tax, withheld FROM report.tax_estimate WHERE year = 2026"))
    assert [s["person"] for s in stored] == ["Ola Eksempel Nordmann"] and stored[0]["total_tax"] > 0
    slips = run(conn.fetch("SELECT employer, tax_table FROM report.payslips"))
    assert slips == [{"employer": "EKSEMPELETATEN", "tax_table": "8888"}]
    # A second person: a Household column adds the two up
    monkeypatch.setattr(payslips, "pdf_text", lambda content: payslip_text.layout_b())
    run(fileimport.import_file(conn, "slip-b.pdf", b"%PDF-slip-b"))
    page = run(Client(app, client_ip="192.168.1.20").get("/tax?year=2026"))
    assert "Household</th>" in page.text
    totals = run(conn.fetch("SELECT total_tax FROM report.tax_estimate WHERE year = 2026"))
    both = f"{round(sum(t['total_tax'] for t in totals)):,}".replace(",", "\u00a0")
    assert len(totals) == 2 and both in page.text.replace(" ", "\u00a0")


def test_an_expense_claim_splits_its_payout(web, monkeypatch):
    from heimkunnskap import claims, manual, payslips
    from tests import claim_text
    conn = PsqlConn(PG)
    bank = run(conn.fetchval("INSERT INTO account (account_key, source, aspsp_name, cash_account_type) "
                             "VALUES ('c-bank', 'enable_banking', 'DNB', 'CACC') RETURNING id"))
    card = run(conn.fetchval("INSERT INTO account (account_key, source, cash_account_type) "
                             "VALUES ('c-card', 'file', 'CARD') RETURNING id"))
    batch = run(conn.fetchval("INSERT INTO import_batch (source, kind) VALUES ('t', 't') RETURNING id"))
    ids = {}
    for i, (key, acct, day, amount, text) in enumerate([
            ("flight", card, "2026-03-01", -250000, "Scandinavian Airline BE Oslo"),
            ("taxi", card, "2026-05-04", -49100, "SumUp *TAXIS PARIS, Paris, FRA"),
            ("dinner", card, "2026-05-05", -50000, "RESTAURANT EKSEMPEL, PARIS"),
            ("train", card, "2026-05-06", -20000, "Vipps*FLYTOGET AS, Oslo"),
            ("hotel", card, "2026-05-07", -1024600, "HOTEL EKSEMPEL PARIS"),
            ("payout", bank, "2026-06-19", 1580000, "Lønn, Eksempeletaten")]):
        ids[key] = run(conn.fetchval(
            "INSERT INTO txn (account_id, booking_date, amount_minor, currency, description, "
            "fingerprint, batch_id) VALUES ($1, $2, $3, 'NOK', $4, $5, $6) RETURNING id",
            acct, day, amount, text, f"cl{i}", batch))
    run(pairing.run(conn))
    june = "SELECT income, spending FROM report.household_monthly WHERE month = '2026-06-01'"
    assert run(conn.fetchrow(june)) == {"income": 15800, "spending": 0}

    monkeypatch.setattr(payslips, "pdf_text", lambda content: "not a pay slip")
    texts = {b"%PDF-c1": claim_text.abroad(), b"%PDF-c2": claim_text.abroad(hotel="10000,00 ")}
    monkeypatch.setattr(claims, "pdf_text", lambda content: (texts[content], False))
    r = run(fileimport.import_file(conn, "expense_100001.pdf", b"%PDF-c1"))
    assert r["status"] == "imported" and r["kind"] == "expense_claim"
    assert "paid out 15 800.00 on 19.06.2026" in r["checks"]
    assert "4 of 4 outlays found among the card payments" in r["checks"]

    # The payout pays back the hotel as claimed (the 246 kr extra on the bill stays
    # spending), the flight, the taxi as paid and the airport train; the rest is income
    back = 1000000 + 250000 + 49100 + 20000
    assert run(conn.fetchrow(june)) == {"income": (1580000 - back) / 100, "spending": -back / 100}
    cats = {r["category"]: r["spending"] for r in run(conn.fetch(
        "SELECT category, spending FROM report.category_monthly WHERE month = '2026-06-01'"))}
    train_cat = run(conn.fetchval(
        "SELECT c.name FROM txn_category tc JOIN category c ON c.id = tc.category_id "
        "WHERE tc.txn_id = $1", ids["train"]))
    assert cats["Travel"] == -(1000000 + 250000 + 49100 + (20000 if train_cat == "Travel" else 0)) / 100
    hotel = run(conn.fetchrow("SELECT match, txn_id FROM report.expense_claim_lines "
                              "WHERE kind = 'hotel'"))
    assert hotel == {"match": "auto", "txn_id": ids["hotel"]}

    report = run(manual.claims_report(conn))
    assert "Claim 100001, Ola Eksempel Nordmann" in report and "paid: 15 800.00" in report
    train_line = run(conn.fetchval("SELECT id FROM expense_claim_line WHERE kind = 'train'"))
    run(manual.claim_line(conn, train_line, "none"))              # say it was not paid by card
    assert run(conn.fetchval("SELECT txn_id FROM expense_claim_line WHERE id = $1", train_line)) is None
    assert run(conn.fetchrow(june))["spending"] == -back / 100     # still paid back, as Travel
    run(manual.claim_line(conn, train_line, str(ids["train"])))
    assert "(set by hand)" in run(manual.claims_report(conn))
    with pytest.raises(SystemExit):
        run(manual.claim_line(conn, train_line, str(ids["payout"])))      # money in

    run(manual.claim_payout(conn, "100001", "none"))
    assert run(conn.fetchrow(june)) == {"income": 15800, "spending": 0}
    assert "paid: not found; expected a deposit of 15 800.00" in run(manual.claims_report(conn))
    run(manual.claim_payout(conn, "100001", "auto"))
    assert run(conn.fetchrow(june))["spending"] == -back / 100
    run(pairing.run(conn))                                         # a rerun changes nothing
    assert run(conn.fetchrow(june))["spending"] == -back / 100

    assert run(fileimport.import_file(conn, "again.pdf", b"%PDF-c1"))["status"] == "duplicate"
    again = run(fileimport.import_file(conn, "expense_100001 (1).pdf", b"%PDF-c2"))
    assert again["status"] == "imported"                           # the same claim, replaced
    assert run(conn.fetchval("SELECT count(*) FROM expense_claim")) == 1
    assert run(conn.fetchval("SELECT count(*) FROM expense_claim_line")) == 7

    monkeypatch.setattr(claims, "pdf_text",
                        lambda content: (claim_text.abroad(hotel="10001,00"), True))
    bad = run(fileimport.import_file(conn, "scan.pdf", b"%PDF-scan"))
    assert bad["status"] == "rejected" and "misread (scanned, read by OCR)" in bad["reason"]
    monkeypatch.setattr(claims, "pdf_text", lambda content: ("something else", False))
    other = run(fileimport.import_file(conn, "letter.pdf", b"%PDF-letter"))
    assert other["status"] == "rejected" and "not an expense claim either" in other["reason"]


def test_a_claim_by_mail(web, monkeypatch):
    import dataclasses
    from heimkunnskap import claims, mailin, payslips
    from tests import claim_text
    from tests.test_mailin import message
    conn = PsqlConn(PG)
    monkeypatch.setattr(mailin, "settings", dataclasses.replace(
        mailin.settings, mail_user="intake@example.com", mail_allowed_senders=("ola@example.org",)))
    monkeypatch.setattr(payslips, "pdf_text", lambda content: "not a pay slip")
    monkeypatch.setattr(claims, "pdf_text", lambda content: (claim_text.abroad(), False))
    raw = message(files=[("expense_100001.pdf", b"%PDF-1.7 claim", "application/pdf")])
    h = run(mailin.handle(conn, raw))
    assert h.imported and h.results[0]["kind"] == "expense_claim"
    assert "payout was not found" in " ".join(h.results[0]["checks"])
    assert run(conn.fetchval("SELECT kind FROM import_batch WHERE source = 'expense_claim'")) == "mail"
    forged = message(sender="ola@example.org", results=("mx.google.com; dmarc=fail header.from=example.org",),
                     files=[("expense_100002.pdf", b"%PDF-1.7 other", "application/pdf")])
    h = run(mailin.handle(conn, forged))
    assert not h.imported and h.results == []
    assert run(conn.fetchval("SELECT count(*) FROM expense_claim")) == 1


def test_business_expenses_and_vat_terms(web, monkeypatch):
    from heimkunnskap import business, manual, notify, payslips, tax
    from tests import payslip_text
    app, _, _ = web
    conn = PsqlConn(PG)
    sent = []
    monkeypatch.setattr(notify, "send", lambda title, body="", priority=0: sent.append((title, body)))
    biz = run(conn.fetchval("INSERT INTO account (account_key, source, cash_account_type, name, "
                            "is_business_income) VALUES ('b-biz', 'enable_banking', 'CACC', "
                            "'Ola Eksempel Nordmann', true) RETURNING id"))
    bank = run(conn.fetchval("INSERT INTO account (account_key, source, aspsp_name, cash_account_type) "
                             "VALUES ('b-bank', 'enable_banking', 'DNB', 'CACC') RETURNING id"))
    card = run(conn.fetchval("INSERT INTO account (account_key, source, cash_account_type) "
                             "VALUES ('b-card', 'file', 'CARD') RETURNING id"))
    batch = run(conn.fetchval("INSERT INTO import_batch (source, kind) VALUES ('t', 't') RETURNING id"))
    ids = {}
    for i, (key, acct, day, amount, text) in enumerate([
            ("before", biz, "2026-04-20", 3000000, "faktura 2 Overføring Innland, Kunde AS"),
            ("march", biz, "2026-05-15", 5500000, "faktura 3 Overføring Innland, Kunde AS"),
            ("phone", card, "2026-03-01", -50000, "TELEFON AS"),
            ("software", card, "2026-05-10", -125000, "ADOBE SYSTEMS"),
            ("chair", card, "2026-06-01", -254000, "KONTORSTOL AS"),
            ("paid", bank, "2026-06-25", -1500000, "11111111111111 Giro, Innkrevingsmynd.-Mva, Nettgiro"),
            ("refund", biz, "2026-08-18", 75800, "Orgnr:999999999, 2026/03, MVA Tilgode Overføring "
                                                 "Innland, Skatteetaten"),
            ("aug", biz, "2026-10-01", 1250000, "Timer august Overføring Innland, Kunde AS"),
            ("sep", biz, "2026-10-09", 5500000, "faktura 5 Overføring Innland, Kunde AS")]):
        ids[key] = run(conn.fetchval(
            "INSERT INTO txn (account_id, booking_date, amount_minor, currency, description, "
            "fingerprint, batch_id) VALUES ($1, $2, $3, 'NOK', $4, $5, $6) RETURNING id",
            acct, day, amount, text, f"bz{i}", batch))
    run(pairing.run(conn))
    run(manual.set_value(conn, "vat_registered_from", "2026-04-19"))
    run(manual.set_vat(conn, ids["before"], 0, "before VAT"))
    run(manual.set_vat(conn, ids["march"], 15000, "incl. March", term="2026-04"))
    with pytest.raises(SystemExit):
        run(manual.set_vat(conn, ids["march"], 15000, None, term="April"))

    # A rule, a tag by hand with the VAT given, a share before registration (no input VAT)
    assert run(manual.business_rule(conn, "adobe", 1.0, 25, "software")) == "rule 1: 1 payments tagged"
    run(manual.tag_expense(conn, ids["chair"], 1.0, 25, 508, "office chair"))
    run(manual.tag_expense(conn, ids["phone"], 0.5, 25, None, None))
    with pytest.raises(SystemExit):
        run(manual.tag_expense(conn, ids["chair"], 1.5, 25, None, None))
    with pytest.raises(SystemExit):
        run(manual.untag_expense(conn, ids["software"]))                           # by rule
    exp = {r["id"]: r for r in run(conn.fetch("SELECT * FROM report.business_expenses"))}
    assert (exp[ids["software"]]["input_vat"], exp[ids["software"]]["cost_excl_vat"]) == (250, 1000)
    assert exp[ids["chair"]]["input_vat"] == 508
    assert (exp[ids["phone"]]["cost"], exp[ids["phone"]]["input_vat"]) == (250, 0)
    cats = {r["txn_id"]: r["name"] for r in run(conn.fetch(
        "SELECT tc.txn_id, c.name FROM txn_category tc JOIN category c ON c.id = tc.category_id"))}
    assert cats[ids["software"]] == "Business" and cats[ids["phone"]] != "Business"

    terms = {t["name"]: t for t in run(business.terms(conn, date(2026, 11, 22)))}
    assert list(terms) == ["March-April 2026", "May-June 2026", "July-August 2026",
                           "September-October 2026", "November-December 2026"]
    assert (terms["March-April 2026"]["output_vat"], terms["March-April 2026"]["status"]) == (15000, "settled")
    may = terms["May-June 2026"]
    assert (may["input_vat"], may["net"], may["paid"], may["status"]) == (758, -758, -758, "settled")
    sep = terms["September-October 2026"]
    assert (sep["output_vat"], sep["outstanding"], sep["due"], sep["status"]) == (
        13500, 13500, date(2026, 12, 10), "due")
    assert terms["November-December 2026"]["status"] == "running"

    # Reminders: when the term has ended, three days before, and overdue; each once
    assert run(business.check_alerts(conn, date(2026, 11, 22))) == [
        "VAT for September-October 2026: about 13 500 kr"]
    assert run(business.check_alerts(conn, date(2026, 11, 23))) == []
    assert run(business.check_alerts(conn, date(2026, 12, 8))) == [
        "VAT for September-October 2026: about 13 500 kr"]
    assert "/business" in sent[0][1] and "Due 10.12.2026" in sent[0][1]
    run(conn.execute("INSERT INTO txn (account_id, booking_date, amount_minor, currency, description, "
                     "fingerprint, batch_id) VALUES ($1, '2026-12-09', -1350000, 'NOK', "
                     "'Giro, Innkrevingsmynd.-Mva, Nettgiro', 'bz-pay', $2)", bank, batch))
    run(pairing.run(conn))
    assert run(business.check_alerts(conn, date(2026, 12, 11))) == []           # paid: not overdue

    # The tax estimate: income less VAT, less the expenses excl. VAT
    monkeypatch.setattr(payslips, "pdf_text", lambda content: payslip_text.dfo())
    run(fileimport.import_file(conn, "dfo.pdf", b"%PDF-dfo-biz"))
    [ola] = [i for i in run(tax.gather(conn, 2026, today=date(2026, 11, 22)))
             if i.person == "Ola Eksempel Nordmann"]
    income = 30000 + 55000 + 12500 + 55000
    vat = 0 + 15000 + 2500 + 11000
    costs = 1000 + (2540 - 508) + 250
    assert ola.business_profit == (income - vat - costs) * 100
    assert any("3 tagged expenses" in n for n in ola.notes)

    report = run(manual.business_report(conn, 2026))
    assert "September-October 2026" in report and "office" not in report.split("Rules:")[1]
    page = run(Client(app, client_ip="192.168.1.20").get("/business?q=kontor"))
    assert page.status == 200 and "September-October 2026" in page.text and "KONTORSTOL" in page.text
    run(manual.business_rule_delete(conn, 1))
    assert ids["software"] not in {r["id"] for r in run(conn.fetch("SELECT id FROM report.business_expenses"))}


def test_eurobonus_export_and_points(web):
    from heimkunnskap import manual, points
    from tests import eurobonus_file
    conn = PsqlConn(PG)
    dnb = run(conn.fetchval("INSERT INTO account (account_key, source, cash_account_type, name) "
                            "VALUES ('file:dnb_mastercard', 'file', 'CARD', 'DNB Mastercard') RETURNING id"))
    amex = run(conn.fetchval("INSERT INTO account (account_key, source, cash_account_type, name) "
                             "VALUES ('file:amex:12345', 'file', 'CARD', 'American Express') RETURNING id"))
    batch = run(conn.fetchval("INSERT INTO import_batch (source, kind) VALUES ('t', 't') RETURNING id"))
    for i, (acct, day, kr, text, cur) in enumerate([
            (amex, "2026-10-01", -1000, "KIWI 100 EKSEMPEL", None),
            (dnb, "2026-09-26", -5000, "AER*KIWI 100 EKSEMPELPO", None),      # Amex known at Kiwi
            (dnb, "2026-10-10", -5000, "HOTEL EKSEMPEL PARIS", "EUR"),
            (dnb, "2026-10-12", -2000, "Vipps*Ola Nordmann, Oslo", None),    # a transfer
            (dnb, "2026-10-26", -11000, "BAKERI EKSEMPEL", None)]):           # the November posting
        run(conn.execute(
            "INSERT INTO txn (account_id, booking_date, amount_minor, currency, original_currency, "
            "description, fingerprint, batch_id) VALUES ($1, $2, $3, 'NOK', $4, $5, $6, $7)",
            acct, day, kr * 100, cur, text, f"eb{i}", batch))
    run(pairing.run(conn))
    content = eurobonus_file.export()
    r = run(fileimport.import_file(conn, "Transactions.xlsx", content))
    assert (r["status"], r["kind"], r["rows"], r["added"]) == ("imported", "eurobonus", 10, 10)
    assert r["period"] == "23.06.2017-23.11.2026"
    again = eurobonus_file.export() + b""                      # a new export: same rows, new bytes
    assert run(fileimport.import_file(conn, "Transactions (1).xlsx", content))["status"] == "duplicate"
    monthly = {(m["card"], m["period"]): m for m in run(conn.fetch("SELECT * FROM report.points_monthly"))}
    oct_ = monthly[("dnb", date(2026, 10, 1))]
    # 5,000 kr at 15 (domestic) and 5,000 kr at 20 (foreign); the Vipps transfer earns nothing
    assert (oct_["expected"], oct_["received"], oct_["difference"]) == (1750, 1500, -250)
    assert monthly[("dnb", date(2026, 11, 1))]["expected"] == 1650
    assert monthly[("dnb", date(2026, 11, 1))]["received"] == 1650
    assert monthly[("amex", date(2026, 10, 1))]["expected"] == 200
    assert monthly[("amex", date(2026, 11, 1))]["received"] == 400
    kiwi = run(conn.fetchrow("SELECT * FROM report.points_purchases WHERE description LIKE 'AER*KIWI%'"))
    assert (kiwi["points"], kiwi["best_card"], kiwi["missed"]) == (750, "amex", 250)
    vipps = run(conn.fetchrow("SELECT * FROM report.points_purchases WHERE description LIKE 'Vipps%'"))
    assert vipps["excluded"] == "Vipps to a person: a money transfer" and vipps["points"] == 0
    checks = run(points.import_checks(conn, today=date(2026, 11, 22)))
    assert "newest posting 23.11.2026" in checks
    assert any(c.startswith("DNB Mastercard (Upgrade SAGA), October 2026: 1 500 points received, "
                            "1 750 expected") for c in checks)
    report = run(manual.points_report(conn, 2026))
    assert "DNB Mastercard" in report and "Missed" in report and "Level points" in report
    assert "Award travel kept: 190 000 points on 1 bookings" in report


def test_points_page(web):
    from tests import eurobonus_file
    app, _, _ = web
    conn = PsqlConn(PG)
    empty = run(Client(app, client_ip="192.168.1.20").get("/points?year=2026"))
    assert empty.status == 200 and "No EuroBonus history yet" in empty.text
    run(fileimport.import_file(conn, "Transactions.xlsx", eurobonus_file.export()))
    page = run(Client(app, client_ip="192.168.1.20").get("/points?year=2026"))
    assert page.status == 200 and "NorgesGruppen" in page.text and "Level points" in page.text
    assert "Award travel kept" in page.text and "Oslo - Bangkok" in page.text
    flows = {r["partner"]: r["flow"] for r in run(conn.fetch("SELECT partner, flow FROM report.eurobonus"))}
    assert flows["Oslo - Bangkok"] == "award" and flows["DNB Rewards"] == "earned"
    assert flows["Level Points from DNB Rewards"] == "level"


def test_trumf_fetch_match_and_page(web, monkeypatch):
    from heimkunnskap import manual, notify, trumf
    from tests import trumf_pages
    app, _, _ = web
    alerts = []
    monkeypatch.setattr(notify, "send", lambda title, body="", priority=0: alerts.append(title))
    conn = PsqlConn(PG)
    assert run(trumf.daily(conn)) is None                               # no login stored: nothing
    page = run(Client(app, client_ip="192.168.1.20").get("/groceries"))
    assert page.status == 200 and "No trumf.no login stored" in page.text
    assert "Nothing stored" in run(manual.trumf_session(conn, "_ga=1; other=2"))
    assert "1 cookie piece" in run(manual.trumf_session(
        conn, "_ga=1; __Secure-authjs.session-token=old; consent=1"))
    card = run(conn.fetchval("INSERT INTO account (account_key, source, cash_account_type, name) "
                             "VALUES ('file:amex:12345', 'file', 'CARD', 'American Express') RETURNING id"))
    batch = run(conn.fetchval("INSERT INTO import_batch (source, kind) VALUES ('t', 't') RETURNING id"))
    for i, (day, kr, text) in enumerate([("2026-11-18", -1045.0, "MENY EKSEMPELSENTERET"),
                                         ("2026-11-21", -312.4, "NORGESGRUPPEN*KIWI 123"),
                                         ("2026-11-21", -312.4, "ANNEN BUTIKK")]):
        run(conn.execute(
            "INSERT INTO txn (account_id, booking_date, amount_minor, currency, description, fingerprint, "
            "batch_id) VALUES ($1, $2, $3, 'NOK', $4, $5, $6)", card, day, round(kr * 100), text, f"tr{i}", batch))
    site = trumf_pages.FakeSite()
    result = run(trumf.run(conn, client=site, delay=0))
    assert result == {"status": "ok", "purchases": 6, "new": 6, "receipts": 2, "lines": 4, "left": 0}
    assert ("/trumf-profil/kvitteringer", False) in site.paths and ("/trumf-profil/kvitteringer", True) in site.paths
    assert run(trumf.load_jar(conn)) == {"__Secure-authjs.session-token": "renewed"}
    rows = {r["store"]: r for r in run(conn.fetch("SELECT * FROM report.trumf_purchases"))}
    kiwi = rows["KIWI Eksempelveien"]
    assert (kiwi["amount"], kiwi["bonus"], kiwi["lines"], kiwi["paid_with"]) == (312.4, 3.12, 3, "American Express")
    kiwi_txn = run(conn.fetchval("SELECT description FROM txn WHERE id = $1", kiwi["txn_id"]))
    assert kiwi_txn == "NORGESGRUPPEN*KIWI 123"                         # the chain decides between equal amounts
    assert rows["MENY Eksempelsenteret"]["txn_id"] is not None
    transfers = run(conn.fetch("SELECT batch_id, bonus, txn_id FROM report.trumf_purchases "
                               "WHERE category = 'CONSUME' ORDER BY batch_id"))
    assert [t["bonus"] for t in transfers] == [-200, -200, -250]        # each its own, without a batchId
    assert transfers[1]["batch_id"].endswith(":2") and all(t["txn_id"] is None for t in transfers)
    lines = run(conn.fetch("SELECT * FROM report.trumf_lines ORDER BY batch_id, line_no"))
    bananas = next(l for l in lines if l["product"] == "Bananer")
    assert (bananas["quantity"], bananas["unit"], bananas["amount"]) == (1.234, "KG", 32.5)
    assert next(l for l in lines if l["product"] == "Laks filet")["bonus_rules"] == "Dobbel bonus fisk"
    # A second run reads no receipt twice
    again = trumf_pages.FakeSite()
    assert run(trumf.run(conn, client=again, delay=0))["receipts"] == 0
    assert not any(p.startswith("/trumf-profil/kvitteringer/") for p, _ in again.paths)
    report = run(manual.trumf_report(conn, fetch=False))
    assert "KIWI" in report and "Trumf bonus moved out (to EuroBonus): 650.00 kr" in report
    page = run(Client(app, client_ip="192.168.1.20").get("/groceries?months=24"))
    assert page.status == 200 and "Kaffe filtermalt" in page.text and "Meny" in page.text
    assert "Not counted here, not grocery stores:\n    Trumf Netthandel (web shops) 899 kr (1)" in page.text
    assert "1 357" in page.text and "2 256" not in page.text     # the month: Kiwi and Meny, not the web shop
    # The login ends: one alert, however many days it stays ended
    assert run(trumf.run(conn, client=trumf_pages.FakeSite(ended=True), delay=0))["status"] == "session ended"
    run(trumf.run(conn, client=trumf_pages.FakeSite(ended=True), delay=0))
    assert alerts == ["Trumf login has ended"]
    assert "ended" in run(conn.fetchval("SELECT last_error FROM trumf_session"))
    # A changed page: one alert of its own
    assert run(trumf.run(conn, client=trumf_pages.FakeSite(list_text="0:{}\n"), delay=0))["status"] == "failed"
    assert alerts == ["Trumf login has ended", "Trumf fetch failed"]
    # A new login clears the alert state
    run(manual.trumf_session(conn, "__Secure-authjs.session-token.0=a; __Secure-authjs.session-token.1=b"))
    assert run(trumf.run(conn, client=trumf_pages.FakeSite(), delay=0))["status"] == "ok"
    assert run(conn.fetchval("SELECT last_error FROM trumf_session")) is None


def test_a_password_protected_pdf_says_so(web):
    import io
    import pypdf
    w = pypdf.PdfWriter()
    w.add_blank_page(100, 100)
    w.encrypt("010190", algorithm="AES-128")
    b = io.BytesIO()
    w.write(b)
    r = run(fileimport.import_file(PsqlConn(PG), "PayrollSlip.pdf", b.getvalue()))
    assert r["status"] == "rejected" and r["reason"].startswith("the PDF is password-protected")


def test_one_trumf_fetch_at_a_time(web):
    import subprocess
    import time
    from heimkunnskap import trumf
    from tests import trumf_pages
    holder = subprocess.Popen(["psql", PG, "-qc", f"SELECT pg_advisory_lock({trumf.LOCK}), pg_sleep(4)"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1)
        conn = PsqlConn(PG)
        run(trumf.set_session(conn, "__Secure-authjs.session-token=x"))
        site = trumf_pages.FakeSite()
        assert run(trumf.run(conn, client=site, delay=0))["status"].startswith("already running")
        assert site.paths == []
    finally:
        holder.wait()
