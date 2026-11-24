"""The web app and Claude connector, without a database."""

import asyncio

from heimkunnskap.enablebanking import EnableBanking
from heimkunnskap.web.app import bearer_problem, create_app
from tests.asgi import Client
from tests.fakes import FakeApi, key_pair

PRIVATE, _ = key_pair()
TOKEN = "t" * 43


def make(**kw):
    api = FakeApi({("POST", "/auth"): lambda q, b: {"url": "https://bank.test/auth?x=1"}})
    eb = EnableBanking("app", PRIVATE, "https://eb.test", opener=api)
    kw.setdefault("mcp_token", TOKEN)
    kw.setdefault("allowed_networks", ("192.168.1.0/24", "10.8.0.0/24"))
    kw.setdefault("public_url", "https://heim.test")
    app = create_app(secret="s" * 40, eb=eb, **kw)
    return app, api


def run(coro):
    return asyncio.run(coro)


def rpc(method, mid=1, **params):
    return {"jsonrpc": "2.0", "id": mid, "method": method, "params": params}


def test_bearer_problem():
    assert bearer_problem(f"Bearer {TOKEN}", TOKEN) is None
    assert bearer_problem(f"bearer   {TOKEN} ", TOKEN) is None
    assert bearer_problem(TOKEN, TOKEN) is None
    assert bearer_problem(None, TOKEN) == "no Authorization header"
    problem = bearer_problem("Bearer nope", TOKEN)
    assert "4 characters, expected 43" in problem and "nope" not in problem


def test_network_gate_and_headers():
    app, _ = make()
    c = Client(app)
    r = run(c.get("/eb/start?bank=DNB", headers={"x-forwarded-for": "203.0.113.9"}))
    assert r.status == 403
    assert r.headers["x-frame-options"] == "DENY"
    # a spoofed first entry does not help: Apache's entry is the last one
    r = run(c.get("/eb/start?bank=DNB",
                  headers={"x-forwarded-for": "192.168.1.5, 203.0.113.9"}))
    assert r.status == 403
    assert run(c.get("/healthz", headers={"x-forwarded-for": "203.0.113.9"})).status == 200


def test_mcp_auth_and_protocol():
    app, _ = make()
    c = Client(app, client_ip="160.79.104.10")
    bearer = {"authorization": f"Bearer {TOKEN}"}
    assert run(c.post("/mcp", body=rpc("ping"))).status == 401
    assert run(c.post("/mcp", body=rpc("ping"), headers={"authorization": "Bearer x"})).status == 401
    assert run(c.get("/mcp", headers=bearer)).status == 405
    assert run(c.post("/mcp", body=rpc("ping"),
                      headers={**bearer, "origin": "https://evil.test"})).status == 403
    assert run(c.post("/mcp", body="{not json", headers=bearer)).status == 400
    assert run(c.post("/mcp", body=b"x" * 300_000, headers=bearer)).status == 413

    r = run(c.post("/mcp", body=rpc("initialize", protocolVersion="2025-03-26"),
                   headers={"authorization": TOKEN}))           # bare token works too
    assert r.json()["result"]["protocolVersion"] == "2025-03-26"
    assert r.json()["result"]["serverInfo"]["name"] == "heimkunnskap"
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert run(c.post("/mcp", body=note, headers=bearer)).status == 202
    r = run(c.post("/mcp", body=[rpc("tools/list"), note], headers=bearer))
    tools = {t["name"] for t in r.json()[0]["result"]["tools"]}
    assert tools == {"finance_report", "sync_status", "import_log", "tax_estimate", "sql_query"}
    assert all(t["annotations"]["readOnlyHint"] for t in r.json()[0]["result"]["tools"])


def test_mcp_without_token_is_off():
    app, _ = make(mcp_token="")
    r = run(Client(app).post("/mcp", body=rpc("ping"), headers={"authorization": "Bearer "}))
    assert r.status == 404


def test_mcp_without_database_login_reports_error_type_only():
    app, _ = make()
    r = run(Client(app).post("/mcp", body=rpc("tools/call", name="sync_status", arguments={}),
                             headers={"authorization": f"Bearer {TOKEN}"}))
    assert r.json()["error"]["message"] == "internal error: RuntimeError"


def test_consent_start_and_callback_checks():
    app, api = make()
    c = Client(app, client_ip="192.168.1.20")
    assert run(c.get("/eb/start?bank=Nordea")).status == 400
    r = run(c.get("/eb/start?bank=DNB&who=Kari"))
    # a page that moves on by itself: a 303 after a form is blocked by form-action 'self'
    assert r.status == 200
    assert '<meta http-equiv="refresh" content="0; url=https://bank.test/auth?x=1">' in r.text
    assert 'href="https://bank.test/auth?x=1"' in r.text and "for Kari" in r.text
    body = api.calls[-1][3]
    assert body["aspsp"] == {"name": "DNB", "country": "NO"}
    assert body["redirect_url"] == "https://heim.test/eb/callback"
    state = body["state"]

    # wrong state: refused, and the flow is used up
    r = run(c.get("/eb/callback?code=abc&state=wrong"))
    assert r.status == 400 and "did not match" in r.text
    r = run(c.get(f"/eb/callback?code=abc&state={state}"))
    assert r.status == 400 and "not started here" in r.text

    # a browser that never started the flow
    run(c.get("/eb/start?bank=DNB"))
    state = api.calls[-1][3]["state"]
    stranger = Client(app, client_ip="192.168.1.21")
    assert run(stranger.get(f"/eb/callback?code=abc&state={state}")).status == 400

    # the bank says no
    r = run(c.get(f"/eb/callback?error=access_denied&state={state}"))
    assert r.status == 400 and "did not grant" in r.text


def test_cross_site_post_refused():
    app, _ = make()
    c = Client(app, client_ip="192.168.1.20")
    r = run(c.post("/accounts/1/label", body="label=x", headers={
        "origin": "https://evil.test", "content-type": "application/x-www-form-urlencoded"}))
    assert r.status == 403


def test_the_coin_icon_is_served():
    import asyncio
    from heimkunnskap.web.app import create_app
    from tests.asgi import Client

    async def go():
        c = Client(create_app(secret="x" * 32), client_ip="192.168.1.20")
        return await c.get("/favicon.ico"), await c.get("/static/coin.svg")
    ico, svg = asyncio.run(go())
    assert ico.status == 200 and ico.headers.get("content-type") == "image/png"
    assert svg.status == 200 and b"<svg" in svg.body
