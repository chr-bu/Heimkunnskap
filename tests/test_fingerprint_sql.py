"""sql/006_fingerprints.sql must compute exactly what add_fingerprints()
does, or the next download would store every row again. Needs HEIM_TEST_PG."""

import asyncio
import os
import pathlib

import pytest

from heimkunnskap.normalise import add_fingerprints
from tests.pgshim import PsqlConn

PG = os.environ.get("HEIM_TEST_PG")
pytestmark = pytest.mark.skipif(not PG, reason="HEIM_TEST_PG not set")
SQL = (pathlib.Path(__file__).parent.parent / "sql" / "006_fingerprints.sql").read_text()


def run(coro):
    return asyncio.run(coro)


def test_sql_fingerprints_equal_python_and_twins_go():
    conn = PsqlConn(PG)
    run(conn.execute("TRUNCATE txn_class, consent_account, consent, account, import_batch, "
                     "raw_record, txn, balance, app_setting RESTART IDENTITY CASCADE"))
    acct = run(conn.fetchval("INSERT INTO account (account_key, source) VALUES "
                             "('hash-æøå', 'enable_banking') RETURNING id"))
    batch = run(conn.fetchval("INSERT INTO import_batch (source, kind) VALUES ('t', 't') RETURNING id"))
    rows = [
        {"booking_date": "2025-10-15", "amount_minor": -134145, "external_ref": "E1",
         "description": "600000006 Giro, Tibber Norge As, Avtalegiro"},
        {"booking_date": "2025-10-15", "amount_minor": -50220, "external_ref": None,
         "description": "700000007 Giro, Tibber Norge As, Avtalegiro"},
        # two genuine payments, same day, amount and payee, different references
        {"booking_date": "2025-10-16", "amount_minor": -10000, "external_ref": None,
         "description": "111 Giro, Bærum Kommune"},
        {"booking_date": "2025-10-16", "amount_minor": -10000, "external_ref": None,
         "description": "222-3 Giro, Bærum Kommune"},
        # identical card purchases and a text that starts with a year
        {"booking_date": "2025-10-17", "amount_minor": -4500, "external_ref": None,
         "description": "TESTKAFE AS, BÆRUM"},
        {"booking_date": "2025-10-17", "amount_minor": -4500, "external_ref": None,
         "description": "TESTKAFE AS, BÆRUM"},
        {"booking_date": "2025-10-18", "amount_minor": -9900, "external_ref": None,
         "description": "2025 Abonnement  Avis"},
    ]
    add_fingerprints("hash-æøå", rows)
    for r in rows:
        run(conn.execute(
            "INSERT INTO txn (account_id, booking_date, amount_minor, currency, description, "
            "external_ref, fingerprint, batch_id) VALUES ($1, $2, $3, 'NOK', $4, $5, $6, $7)",
            acct, r["booking_date"], r["amount_minor"], r["description"], r["external_ref"],
            r["fingerprint"], batch))
    # the twin DNB sent through the other consent: no reference, new bank reference
    run(conn.execute(
        "INSERT INTO txn (account_id, booking_date, amount_minor, currency, description, "
        "external_ref, fingerprint, batch_id) VALUES ($1, '2025-10-15', -134145, 'NOK', "
        "'Giro, Tibber Norge As, Avtalegiro', 'E9', 'old-style', $2)", acct, batch))

    python = {r["description"]: r["fingerprint"] for r in rows}
    run(conn.execute("BEGIN; " + SQL + " COMMIT"))
    stored = run(conn.fetch("SELECT description, fingerprint FROM txn ORDER BY id"))
    assert len(stored) == len(rows)                           # the twin is gone
    by_text = {}
    for s in stored:
        by_text.setdefault(s["description"], []).append(s["fingerprint"])
    for text, fps in by_text.items():
        want = sorted(r["fingerprint"] for r in rows if r["description"] == text)
        assert sorted(fps) == want, text
    assert python["111 Giro, Bærum Kommune"] != python["222-3 Giro, Bærum Kommune"]
