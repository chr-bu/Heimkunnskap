import pytest

from heimkunnskap import trumf
from tests import trumf_pages


def test_session_cookies_from_a_cookie_header():
    header = ("_ga=GA1.1; __Secure-authjs.callback-url=https%3A%2F%2Fwww.trumf.no; "
              "__Secure-authjs.session-token.0=aaa; __Secure-authjs.session-token.1=bbb; consent=1")
    assert trumf.session_cookies(header) == {"__Secure-authjs.session-token.0": "aaa",
                                             "__Secure-authjs.session-token.1": "bbb"}
    assert trumf.session_cookies("Cookie: __Secure-authjs.session-token=abc\n") == {}  # not a header line
    assert trumf.session_cookies("__Secure-authjs.session-token=abc\n") == {
        "__Secure-authjs.session-token": "abc"}
    assert trumf.session_cookies("_ga=1; other=2") == {}


def test_a_renewed_session_replaces_its_pieces():
    jar = {"__Secure-authjs.session-token.0": "a", "__Secure-authjs.session-token.1": "b"}
    changed = trumf.apply_set_cookies(jar, [
        "__Secure-authjs.session-token=new; Path=/; Expires=Wed, 23 Nov 2027 08:21:10 GMT; HttpOnly; Secure",
        "__Secure-authjs.session-token.0=; Path=/; Max-Age=0",
        "__Secure-authjs.session-token.1=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
        "_ga=ignored; Path=/"])
    assert changed and jar == {"__Secure-authjs.session-token": "new"}
    assert not trumf.apply_set_cookies(jar, ["__Secure-authjs.session-token=new; Path=/"])


def test_the_receipt_list():
    items = trumf.parse_list(trumf_pages.rsc_list())
    assert [i["batchId"] for i in items] == [p["batchId"] for p in trumf_pages.PURCHASES]
    assert items[0]["transaksjonsTidspunkt"] == "2026-11-20T15:02:11.000Z"   # $D taken off
    assert items[3]["transaksjonKategori"] == "CONSUME"
    with pytest.raises(trumf.TrumfError, match="no transactionsData"):
        trumf.parse_list("0:{}\n")


def test_a_receipt_in_an_html_page():
    p = trumf_pages.PURCHASES[0]
    details = trumf.parse_receipt(trumf_pages.receipt_html(p, trumf_pages.RECEIPTS[p["batchId"]]))
    assert details["batchId"] == p["batchId"] and details["ordreId"] is None
    assert [v["produktBeskrivelse"] for v in details["varelinjer"]] == [
        "Melk lett 1l", "Bananer", "Kaffe filtermalt"]
    assert details["varelinjer"][1]["enhetsType"] == "KG"


def test_receipt_path():
    path = trumf.receipt_path(trumf_pages.PURCHASES[0] | {"transaksjonsTidspunkt": "2026-11-20T15:02:11.000Z"})
    assert path.startswith("/trumf-profil/kvitteringer/2611200000000000000000000001?transactionType=ACCRUAL&")
    assert "description=KIWI+Eksempelveien" in path


def test_money():
    assert trumf.minor(312.4) == 31240 and trumf.minor(None) == 0 and trumf.minor(-250) == -25000


def test_what_goes_to_the_database_has_the_types_asyncpg_wants():
    """asyncpg (unlike the psql test shim) refuses text for timestamptz and
    wants Decimal-able numbers."""
    import asyncio
    import json
    from datetime import datetime
    from decimal import Decimal

    class Capture:
        def __init__(self):
            self.rows = []

        async def fetchval(self, *a):
            return 0

        async def execute(self, *a):
            pass

        async def executemany(self, sql, rows):
            self.rows += rows

        def transaction(self):
            class T:
                async def __aenter__(s): pass
                async def __aexit__(s, *a): pass
            return T()

    conn = Capture()
    items = trumf.parse_list(trumf_pages.rsc_list())
    asyncio.run(trumf.store_purchases(conn, items))
    for row in conn.rows:
        assert isinstance(row[2], datetime) and row[2].tzinfo is not None
        assert all(isinstance(x, int) for x in row[6:9]) and isinstance(row[9], bool)
        json.loads(row[10])
    p = trumf_pages.PURCHASES[0]
    conn = Capture()
    details = trumf.parse_receipt(trumf_pages.receipt_html(p, trumf_pages.RECEIPTS[p["batchId"]]))
    asyncio.run(trumf.store_receipt(conn, p["batchId"], details))
    for row in conn.rows:
        assert isinstance(row[4], Decimal) and isinstance(row[8], Decimal) and isinstance(row[9], Decimal)
        assert isinstance(row[3], str) and isinstance(row[6], int)
    assert conn.rows[1][4] == Decimal("1.234")
