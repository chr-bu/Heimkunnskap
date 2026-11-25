"""Sync against a real PostgreSQL with the schema applied, through psql.

    HEIM_TEST_PG="host=/path port=5432 user=heimkunnskap dbname=heimkunnskap" \
    HEIM_TEST_PG_RO="host=/path port=5432 user=heimkunnskap_ro dbname=heimkunnskap" pytest

Skipped without HEIM_TEST_PG. The tables are emptied first: never point
this at a database holding real data."""

import asyncio
import dataclasses
import os

import pytest

from heimkunnskap import db, notify, sync
from heimkunnskap.enablebanking import EnableBanking
from tests.fakes import FakeApi, key_pair
from tests.pgshim import PsqlConn

PG = os.environ.get("HEIM_TEST_PG")
PG_RO = os.environ.get("HEIM_TEST_PG_RO")
pytestmark = pytest.mark.skipif(not PG, reason="HEIM_TEST_PG not set")

PRIVATE, _ = key_pair()
BUSINESS = "12345678903"          # synthetic


def tx(amount, date, text, ref=None, kind="DBIT", status="BOOK"):
    return {"transaction_amount": {"amount": amount, "currency": "NOK"},
            "credit_debit_indicator": kind, "status": status, "booking_date": date,
            "remittance_information": [text], "entry_reference": ref}


class Bank:
    """Two accounts; the first returns its transactions over two pages."""

    def __init__(self):
        self.refs_suffix = ""
        self.balance_status = 200
        self.page1 = [tx("100.00", "2026-11-10", "Kaffe"), tx("100.00", "2026-11-10", "Kaffe"),
                      tx("59.90", "2026-11-11", "Pending", status="PDNG")]
        self.page2 = [tx("2500.00", "2026-11-12", "Strøm", ref="R1"),
                      tx("30000.00", "2026-11-12", "Lønn", ref="R2", kind="CRDT")]
        self.business = [tx("5000.00", "2026-11-05", "Faktura 17", ref="B1", kind="CRDT")]

    def routes(self):
        return {
            ("GET", "/sessions/S1"): lambda q, b: {
                "aspsp": {"name": "DNB", "country": "NO"},
                "access": {"valid_until": "2027-05-14T10:00:00+00:00"},
                "accounts": ["u1", "u2"]},
            ("GET", "/accounts/u1/details"): lambda q, b: {
                "identification_hash": "hash-u1", "account_id": {"iban": "NO9386011117947"},
                "cash_account_type": "CACC", "product": "Brukskonto", "currency": "NOK"},
            ("GET", "/accounts/u2/details"): lambda q, b: {
                "identification_hash": "hash-u2", "cash_account_type": "CACC",
                "all_account_ids": [{"identification": BUSINESS, "scheme_name": "BBAN"}],
                "product": "Brukskonto", "currency": "NOK"},
            ("GET", "/accounts/u1/transactions"): self.u1,
            ("GET", "/accounts/u2/transactions"): lambda q, b: {"transactions": self.business},
            ("GET", "/accounts/u1/balances"): self.balances,
            ("GET", "/accounts/u2/balances"): self.balances,
        }

    def u1(self, q, b):
        if q.get("continuation_key") == "k2":
            page = [dict(t, entry_reference=(t["entry_reference"] or "") + self.refs_suffix
                         if t["entry_reference"] else None) for t in self.page2]
            return {"transactions": page}
        return {"transactions": self.page1, "continuation_key": "k2"}

    def balances(self, q, b):
        if self.balance_status != 200:
            return self.balance_status, {"message": "Session expired"}
        return {"balances": [{"balance_amount": {"amount": "1234.56", "currency": "NOK"},
                              "balance_type": "CLAV", "reference_date": "2026-11-14"}]}


@pytest.fixture
def env(monkeypatch):
    conn = PsqlConn(PG)
    asyncio.run(conn.execute(
        "TRUNCATE trumf_line, trumf_txn, trumf_session, points_expected, eb_txn, business_expense, business_rule, txn_part, expense_claim_line, expense_claim, business_vat, payslip, person, lending, txn_class, consent_account, consent, account, import_batch, raw_record, txn, balance, app_setting "
        "RESTART IDENTITY CASCADE"))
    monkeypatch.setattr(sync, "settings", dataclasses.replace(
        sync.settings, business_income_accounts=(BUSINESS,), public_url="https://heim.test"))
    alerts = []
    monkeypatch.setattr(notify, "send", lambda title, body="", priority=0: alerts.append(title))
    bank = Bank()
    eb = EnableBanking("app", PRIVATE, "https://eb.test", opener=FakeApi(bank.routes()))
    return conn, eb, bank, alerts


def run(coro):
    return asyncio.run(coro)


def test_register_full_then_nightly(env):
    conn, eb, bank, alerts = env
    consent_id = run(sync.register_session(conn, eb, "S1"))
    consent = run(conn.fetchrow("SELECT * FROM consent WHERE id = $1", consent_id))
    first = run(sync.sync_consent(conn, eb, consent, full=True))
    assert first["errors"] == []
    assert (first["accounts"], first["seen"], first["added"]) == (2, 5, 5)

    rows = run(conn.fetch("SELECT amount_minor, external_ref FROM txn ORDER BY id"))
    assert sorted(r["amount_minor"] for r in rows) == [-250000, -10000, -10000, 500000, 3000000]

    # Same data again, and the bank's references changed meanwhile: nothing new
    bank.refs_suffix = "-x"
    again = run(sync.sync_consent(conn, eb, consent))
    assert (again["seen"], again["added"]) == (5, 0)

    # A third identical coffee the same day is new
    bank.page1.append(tx("100.00", "2026-11-10", "Kaffe"))
    third = run(sync.sync_consent(conn, eb, consent))
    assert third["added"] == 1

    batches = run(conn.fetch("SELECT kind, error FROM import_batch WHERE account_id IS NOT NULL"))
    assert {b["kind"] for b in batches} == {"full", "nightly"}
    assert all(b["error"] is None for b in batches)
    assert alerts == []


def test_business_account_flag_and_report_views(env):
    conn, eb, bank, _ = env
    consent_id = run(sync.register_session(conn, eb, "S1"))
    consent = run(conn.fetchrow("SELECT * FROM consent WHERE id = $1", consent_id))
    run(sync.sync_consent(conn, eb, consent, full=True))
    if not PG_RO:
        pytest.skip("HEIM_TEST_PG_RO not set")
    ro = PsqlConn(PG_RO)
    accounts = run(ro.fetch("SELECT number_masked, is_business_income FROM report.accounts "
                            "ORDER BY id"))
    assert accounts == [{"number_masked": "…7947", "is_business_income": False},
                        {"number_masked": "…8903", "is_business_income": True}]
    monthly = run(ro.fetch("SELECT money_in, money_out FROM report.monthly WHERE account_id = 1"))
    assert monthly == [{"money_in": 30000.00, "money_out": 2700.00}]
    balances = run(ro.fetch("SELECT amount FROM report.balances WHERE account_id = 2"))
    assert balances == [{"amount": 1234.56}]
    for table in ("public.raw_record", "public.txn", "public.account"):
        with pytest.raises(RuntimeError, match="permission denied for schema public"):
            run(ro.fetch(f"SELECT * FROM {table}"))


def test_expired_consent_alerts_and_stops(env):
    conn, eb, bank, alerts = env
    consent_id = run(sync.register_session(conn, eb, "S1"))
    consent = run(conn.fetchrow("SELECT * FROM consent WHERE id = $1", consent_id))
    bank.balance_status = 401
    result = run(sync.sync_consent(conn, eb, consent))
    assert result["errors"] == ["consent gone"]
    status = run(conn.fetchval("SELECT status FROM consent WHERE id = $1", consent_id))
    assert status == "expired"
    assert alerts == ["DNB: consent no longer valid"]
    errors = run(conn.fetch("SELECT error FROM import_batch WHERE error IS NOT NULL"))
    assert len(errors) == 1


def session(valid_until, *accounts):
    """A POST /sessions response for DNB with these (uid, hash, iban) accounts."""
    return {"aspsp": {"name": "DNB", "country": "NO"},
            "access": {"valid_until": valid_until},
            "accounts": [{"uid": uid, "identification_hash": h, "account_id": {"iban": iban}}
                         for uid, h, iban in accounts]}


U1 = ("u1", "hash-u1", "NO9386011117947")
U2 = ("u2", "hash-u2", "NO0011112222333")
U3 = ("u3", "hash-u3", "NO0044445555666")


def test_renewal_replaces_the_consent_it_covers(env):
    conn, eb, bank, _ = env
    old = run(sync.register_session(conn, eb, "S1"))                       # u1, u2
    renewed = session("2027-06-01T10:00:00+00:00", ("u7", "hash-u1", U1[2]),
                      ("u8", "hash-u2", U2[2]))
    new = run(sync.register_session(conn, eb, "S2", renewed))
    rows = run(conn.fetch("SELECT id, status FROM consent ORDER BY id"))
    assert rows == [{"id": old, "status": "revoked"}, {"id": new, "status": "active"}]
    owned = run(db.consent_accounts(conn, new))
    assert [a["uid"] for a in owned] == ["u7", "u8"]


def test_another_persons_consent_stays_and_each_account_syncs_once(env):
    conn, eb, bank, alerts = env
    ola = run(sync.register_session(conn, eb, "S1", label="Ola"))     # u1, u2
    # Kari reaches u2 (her current account, which Ola may also use) and
    # u3 (her savings, which only she reaches); her consent lasts longer.
    kari = run(sync.register_session(conn, eb, "S9", session(
        "2027-06-01T10:00:00+00:00", ("n2", "hash-u2", U2[2]), ("n3", *U3[1:])), label="Kari"))
    rows = run(conn.fetch("SELECT label, status FROM consent ORDER BY id"))
    assert rows == [{"label": "Ola", "status": "active"},
                    {"label": "Kari", "status": "active"}]
    # u2 is synced under Kari's consent, the one that lasts longer
    assert [a["account_key"] for a in run(db.consent_accounts(conn, ola))] == ["hash-u1"]
    assert [(a["account_key"], a["uid"]) for a in run(db.consent_accounts(conn, kari))] == \
        [("hash-u2", "n2"), ("hash-u3", "n3")]
    counts = run(conn.fetch("SELECT label, accounts FROM report.consents ORDER BY id"))
    assert counts == [{"label": "Ola", "accounts": 2}, {"label": "Kari", "accounts": 2}]
    # Ola renews: his old consent goes, Kari's stays
    run(sync.register_session(conn, eb, "S3", session(
        "2027-07-01T10:00:00+00:00", ("c1", *U1[1:]), ("c2", "hash-u2", U2[2])),
        label="Ola"))
    rows = run(conn.fetch("SELECT session_id, status FROM consent ORDER BY id"))
    assert rows == [{"session_id": "S1", "status": "revoked"},
                    {"session_id": "S9", "status": "active"},
                    {"session_id": "S3", "status": "active"}]


def test_alerts_name_the_person_and_link_their_renewal(env):
    conn, eb, bank, alerts = env
    consent_id = run(sync.register_session(conn, eb, "S1", label="Kari"))
    consent = run(conn.fetchrow("SELECT * FROM consent WHERE id = $1", consent_id))
    bank.balance_status = 401
    run(sync.sync_consent(conn, eb, consent))
    assert alerts == ["DNB (Kari): consent no longer valid"]
    assert sync.consent_link("DNB", "Kari") == "https://heim.test/eb/start?bank=DNB&who=Kari"
    assert sync.consent_link("DNB", None) == "https://heim.test/eb/start?bank=DNB"


def test_account_names_prefer_the_households_nickname(env):
    conn, eb, bank, _ = env
    run(sync.register_session(conn, eb, "S1", session(
        "2027-06-01T10:00:00+00:00", U1, U2)))
    run(conn.execute("UPDATE account SET name = 'Ola Eksempel Nordmann', product = "
                     "'Felles, BRUKSKONTO TILLEGG' WHERE account_key = 'hash-u1'"))
    run(conn.execute("UPDATE account SET name = 'Ola Eksempel Nordmann', product = "
                     "'BRUKSKONTO TILLEGG' WHERE account_key = 'hash-u2'"))
    names = run(conn.fetch("SELECT account FROM report.accounts ORDER BY id"))
    assert names == [{"account": "Felles"}, {"account": "BRUKSKONTO TILLEGG"}]
    run(conn.execute("UPDATE account SET label = 'Felles' WHERE account_key = 'hash-u2'"))
    assert run(conn.fetchval("SELECT account FROM report.accounts WHERE id = 2")) == "Felles"


def test_another_persons_session_keeps_the_nickname(env):
    conn, eb, bank, _ = env
    mine = session("2027-06-01T10:00:00+00:00", U1)
    mine["accounts"][0]["product"] = "Felles, BRUKSKONTO TILLEGG"
    run(sync.register_session(conn, eb, "S1", mine, label="Ola"))
    hers = session("2027-07-01T10:00:00+00:00", ("n1", *U1[1:]))
    hers["accounts"][0]["product"] = "BRUKSKONTO TILLEGG"
    run(sync.register_session(conn, eb, "S9", hers, label="Kari"))
    assert run(conn.fetchval("SELECT account FROM report.accounts")) == "Felles"


class ClosingConn:
    """A connection that closes once the first page has been fetched, as
    the database restarting under the sync does."""

    def __init__(self, conn):
        self.conn, self.closed = conn, False

    def __getattr__(self, name):
        method = getattr(self.conn, name)

        async def call(*args, **kwargs):
            if self.closed:
                raise InterfaceError("connection is closed")
            if name == "fetchval" and "INSERT INTO raw_record" in (args[0] if args else ""):
                self.closed = True
                raise InterfaceError("connection is closed")
            return await method(*args, **kwargs)
        return call


class InterfaceError(Exception):
    """Named like asyncpg's."""


def test_losing_the_database_mid_sync_leaves_it_for_a_retry(env):
    conn, eb, bank, alerts = env
    consent_id = run(sync.register_session(conn, eb, "S1"))
    consent = run(conn.fetchrow("SELECT * FROM consent WHERE id = $1", consent_id))
    closing = ClosingConn(conn)
    with pytest.raises(InterfaceError):
        run(sync.sync_consent(closing, eb, consent))
    # Nothing could be written: the consent keeps its old sync time and the
    # batch stays open, so the worker sees the run as missed
    from datetime import datetime, timezone
    assert run(sync.sync_missed(conn, datetime.now(timezone.utc)))
    open_batches = run(conn.fetchval("SELECT count(*) FROM import_batch WHERE finished_at IS NULL"))
    assert open_batches == 1
    # The next worker start closes it as interrupted
    assert run(sync.mark_interrupted(conn)) == 1
    error = run(conn.fetchval("SELECT error FROM import_batch WHERE kind = 'nightly'"))
    assert error.startswith("interrupted")
    # And the retried sync completes
    again = run(sync.sync_consent(conn, eb, consent))
    assert again["errors"] == [] and again["accounts"] == 2
    assert not run(sync.sync_missed(conn, datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert alerts == []
