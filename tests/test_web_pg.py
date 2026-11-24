"""The consent flow end to end and the MCP tools, against PostgreSQL via psql.
Needs HEIM_TEST_PG, and HEIM_TEST_PG_MCP for the connector (see test_sync_pg)."""

import asyncio
import dataclasses
import os
import re

import pytest

from heimkunnskap import migrate, notify, sync
from heimkunnskap.enablebanking import EnableBanking
from heimkunnskap.web.app import create_app
from tests.asgi import Client
from tests.fakes import FakeApi, key_pair
from tests.pgshim import PsqlConn, PsqlPool
from tests.test_sync_pg import Bank

PG = os.environ.get("HEIM_TEST_PG")
PG_MCP = os.environ.get("HEIM_TEST_PG_MCP")
pytestmark = pytest.mark.skipif(not PG, reason="HEIM_TEST_PG not set")
PRIVATE, _ = key_pair()
TOKEN = "k" * 43


@pytest.fixture
def web(monkeypatch):
    conn = PsqlConn(PG)
    asyncio.run(conn.execute(
        "TRUNCATE trumf_line, trumf_txn, trumf_session, points_expected, eb_txn, business_expense, business_rule, txn_part, expense_claim_line, expense_claim, business_vat, payslip, person, lending, txn_class, consent_account, consent, account, import_batch, raw_record, txn, balance, app_setting "
        "RESTART IDENTITY CASCADE"))
    monkeypatch.setattr(sync, "settings", dataclasses.replace(
        sync.settings, business_income_accounts=("12345678903",)))
    alerts = []
    monkeypatch.setattr(notify, "send", lambda title, body="", priority=0: alerts.append(title))
    bank = Bank()
    routes = bank.routes()
    routes[("POST", "/auth")] = lambda q, b: {"url": "https://bank.test/auth"}
    session = {"session_id": "S1", **routes[("GET", "/sessions/S1")](None, None)}
    session["accounts"] = [
        {"uid": "u1", **routes[("GET", "/accounts/u1/details")](None, None)},
        {"uid": "u2", **routes[("GET", "/accounts/u2/details")](None, None)}]
    routes[("POST", "/sessions")] = lambda q, b: session if b == {"code": "good"} else \
        (422, {"message": "bad code"})
    api = FakeApi(routes)
    eb = EnableBanking("app", PRIVATE, "https://eb.test", opener=api)
    app = create_app(secret="s" * 40, eb=eb, pool=PsqlPool(PG),
                     mcp_pool=PsqlPool(PG_MCP) if PG_MCP else None, mcp_token=TOKEN,
                     allowed_networks=("192.168.1.0/24",), public_url="https://heim.test")
    return app, api, alerts


async def bankid(app, api):
    c = Client(app, client_ip="192.168.1.20")
    await c.get("/eb/start?bank=DNB")
    state = api.calls[-1][3]["state"]
    r = await c.get(f"/eb/callback?code=good&state={state}")
    await asyncio.gather(*app.state.web.tasks)
    return c, r


def test_consent_flow_imports_full_history(web):
    app, api, alerts = web

    async def go():
        c, r = await bankid(app, api)
        assert r.status == 303 and r.headers["location"] == "/"
        page = await c.get("/")
        return page
    page = asyncio.run(go())
    assert page.status == 200
    text = page.text
    assert "Consent stored" in text
    assert "…7947" in text and "…8903" in text and "business income" in text
    assert "1 234,56" in text                       # balance, Norwegian format
    assert "12345678903" not in text and "86011117947" not in text
    # the full import asked for the longest history
    tx_calls = [q for m, p, q, b, a in api.calls if p.endswith("/transactions")]
    assert all(q.get("strategy") == "longest" for q in tx_calls)
    assert alerts == []


def test_label_needs_csrf(web):
    app, api, _ = web

    async def go():
        c, _ = await bankid(app, api)
        page = await c.get("/")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        form = {"content-type": "application/x-www-form-urlencoded"}
        bad = await c.post("/accounts/1/label", body="csrf=nope&label=Felles", headers=form)
        good = await c.post("/accounts/1/label", body=f"csrf={csrf}&label=Felles", headers=form)
        return bad, good, await c.get("/")
    bad, good, page = asyncio.run(go())
    assert bad.status == 303 and good.status == 303
    assert 'value="Felles"' in page.text


def test_bad_code_is_reported(web):
    app, api, _ = web

    async def go():
        c = Client(app, client_ip="192.168.1.20")
        await c.get("/eb/start?bank=DNB")
        state = api.calls[-1][3]["state"]
        return await c.get(f"/eb/callback?code=bad&state={state}")
    r = asyncio.run(go())
    assert r.status == 502 and "ApiError" in r.text


@pytest.mark.skipif(not PG_MCP, reason="HEIM_TEST_PG_MCP not set")
def test_mcp_tools_respect_shared_views(web):
    app, api, _ = web
    owner = PsqlConn(PG)
    asyncio.run(migrate.apply_mcp_grants(owner, ("monthly", "balances", "consents", "imports",
                                                 "accounts", "household_monthly", "review")))
    bearer = {"authorization": f"Bearer {TOKEN}"}

    async def call(c, name, **args):
        r = await c.post("/mcp", headers=bearer, body={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args}})
        res = r.json()["result"]
        return res["content"][0]["text"], res["isError"]

    async def go():
        await bankid(app, api)
        c = Client(app, client_ip="160.79.104.10")
        return {
            "report": await call(c, "finance_report", months=60),
            "status": await call(c, "sync_status"),
            "log": await call(c, "import_log", limit=5),
            "views": await call(c, "sql_query", sql="SELECT account, net FROM monthly ORDER BY 1"),
            "detail": await call(c, "sql_query", sql="SELECT * FROM transactions"),
            "raw": await call(c, "sql_query", sql="SELECT * FROM public.raw_record"),
            "write": await call(c, "sql_query", sql="CREATE TABLE report.x (i int)"),
        }
    out = asyncio.run(go())
    assert not out["report"][1] and "| month | income | spending |" in out["report"][0]
    assert "Latest balances" in out["report"][0] and "1234.56" in out["report"][0]
    assert "DNB" in out["status"][0] and "…8903" in out["status"][0]
    assert "full" in out["log"][0]
    assert not out["views"][1]
    assert out["detail"][1] and "permission denied" in out["detail"][0]
    assert out["raw"][1] and "permission denied" in out["raw"][0]
    assert out["write"][1]
    asyncio.run(migrate.apply_mcp_grants(owner, ("accounts", "monthly", "balances", "consents",
                                                 "imports")))


def test_whose_bankid_travels_through_the_flow(web):
    app, api, _ = web

    async def go():
        c = Client(app, client_ip="192.168.1.20")
        await c.get("/eb/start?bank=DNB&who=%20Kari%20%20")
        state = api.calls[-1][3]["state"]
        await c.get(f"/eb/callback?code=good&state={state}")
        await asyncio.gather(*app.state.web.tasks)
        return await c.get("/")
    page = asyncio.run(go())
    label = asyncio.run(PsqlConn(PG).fetchval("SELECT label FROM consent"))
    assert label == "Kari"
    assert 'href="/eb/start?bank=DNB&amp;who=Kari"' in page.text
    assert 'value="Kari"' in page.text
