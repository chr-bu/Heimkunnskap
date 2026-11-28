"""Heimkunnskap's web app: the BankID consent flow, a status page, account
labels and the Claude connector (/mcp).

Run: python -m heimkunnskap.web. It has no sign-in of its own: Apache
admits the LAN and WireGuard only (docs/deploy.md), and WEB_ALLOWED_NETWORKS
repeats that rule here. The one exception is /mcp, which has its own bearer
token and Apache rule.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import pathlib
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import (FileResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                                 Response)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .. import __version__, categories, db, fileimport, notify, pairing, progress, sync, tax
from ..config import settings
from . import charts
from .mcp import McpServer

log = logging.getLogger("heimkunnskap.web")
HERE = pathlib.Path(__file__).resolve().parent
FORM_MAX_BYTES = 16_000
MCP_MAX_BYTES = 256_000
UPLOAD_MAX_FILES = 24
UPLOAD_MAX_FILE_BYTES = 5_000_000                # a card export is 10–50 kB
UPLOAD_MAX_BYTES = UPLOAD_MAX_FILES * UPLOAD_MAX_FILE_BYTES
CONSENT_FLOW_SECONDS = 900


async def read_limited(request: Request, limit: int) -> bytes | None:
    """The request body, or None if it is larger than limit. Checked against
    Content-Length first and then as the body arrives."""
    try:
        if int(request.headers.get("content-length") or 0) > limit:
            return None
    except ValueError:
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


class Web:
    def __init__(self, pool=None, mcp_pool=None, eb=None, mcp_token: str | None = None,
                 allowed_networks: tuple | None = None, public_url: str | None = None) -> None:
        self._pool = pool
        self._mcp_pool = mcp_pool
        self._eb = eb
        self._lock = asyncio.Lock()
        self.mcp_token = settings.mcp_token if mcp_token is None else mcp_token
        self.public_url = settings.public_url if public_url is None else public_url
        nets = settings.web_allowed_networks if allowed_networks is None else allowed_networks
        self.networks = [ipaddress.ip_network(n, strict=False) for n in nets]
        self.mcp = McpServer(self.mcp_pool)
        self.tasks: set[asyncio.Task] = set()
        self.templates = Jinja2Templates(directory=str(HERE / "templates"))
        self.templates.env.filters["kr"] = fmt_kr
        self.templates.env.filters["kr0"] = fmt_kr0
        self.templates.env.filters["when"] = fmt_when

    # -- plumbing

    async def pool(self):
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    self._pool = await db.create_pool()
        return self._pool

    async def mcp_pool(self):
        """The MCP login, which sees only the report views in MCP_VIEWS."""
        if self._mcp_pool is None:
            if not settings.database_url_mcp:
                raise RuntimeError("DATABASE_URL_MCP is not set")
            async with self._lock:
                if self._mcp_pool is None:
                    self._mcp_pool = await db.create_pool(settings.database_url_mcp)
        return self._mcp_pool

    def eb(self):
        if self._eb is None:
            self._eb = sync.client()
        return self._eb

    def flash(self, request: Request, kind: str, message: str) -> None:
        request.session.setdefault("flash", []).append([kind, message])

    def csrf(self, request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = request.session["csrf"] = secrets.token_urlsafe(24)
        return token

    async def form(self, request: Request) -> dict:
        body = await read_limited(request, FORM_MAX_BYTES)
        if body is None:
            raise PermissionError("form too large")
        data = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"),
                                             keep_blank_values=True).items()}
        sent, expected = data.get("csrf", ""), request.session.get("csrf", "")
        if not expected or not secrets.compare_digest(sent.encode(), expected.encode()):
            raise PermissionError("form expired")
        return data

    def render(self, request: Request, name: str, status_code: int = 200, **ctx) -> Response:
        ctx.update(csrf=self.csrf(request), flashes=request.session.pop("flash", []),
                   version=__version__)
        return self.templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def client_ip(self, request: Request) -> str | None:
        """The browser's address: the last X-Forwarded-For entry (the one
        Apache added) when present, else the peer. The app's port is bound
        to 127.0.0.1, so only Apache can reach it."""
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[-1].strip()
        return request.client.host if request.client else None

    def allowed(self, request: Request) -> bool:
        if not self.networks:
            return True
        try:
            ip = ipaddress.ip_address(self.client_ip(request) or "")
        except ValueError:
            return False
        return any(ip in net for net in self.networks)

    # -- pages

    async def home(self, request: Request) -> Response:
        pool = await self.pool()
        async with pool.acquire() as conn:
            consents = await conn.fetch(
                "SELECT * FROM report.consents ORDER BY bank, valid_until DESC")
            accounts = await conn.fetch(
                """SELECT a.*, acc.label, b.amount AS balance, b.balance_type
                   FROM report.accounts a
                   JOIN account acc ON acc.id = a.id
                   LEFT JOIN LATERAL (SELECT amount, balance_type FROM report.balances
                                      WHERE account_id = a.id ORDER BY as_of DESC LIMIT 1) b ON true
                   ORDER BY a.bank, a.account""")
            imports = await conn.fetch(
                "SELECT * FROM report.imports ORDER BY started_at DESC LIMIT 15")
        consents = [dict(c, renew=sync.consent_link(c["bank"], c["label"]).removeprefix(
                        settings.public_url)) for c in consents]
        banks = [{"short": s, "name": n} for s, (n, _c) in settings.banks.items()]
        running = any(not t.done() for t in self.tasks)
        return self.render(request, "index.html", consents=consents, accounts=accounts,
                           imports=imports, banks=banks, running=running,
                           warn_days=settings.consent_warn_days)

    async def consent_label_post(self, request: Request) -> Response:
        data = await self.form(request)
        label = clean_label(data.get("label"))
        pool = await self.pool()
        async with pool.acquire() as conn:
            await db.set_consent_label(conn, request.path_params["cid"], label)
        return RedirectResponse("/#consents", status_code=303)

    async def label_post(self, request: Request) -> Response:
        data = await self.form(request)
        label = clean_label(data.get("label"))
        pool = await self.pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE account SET label = $2, updated_at = now() WHERE id = $1",
                               request.path_params["aid"], label)
        return RedirectResponse("/#accounts", status_code=303)

    # -- spending categories

    async def tax_page(self, request: Request) -> Response:
        try:
            year = int(request.query_params.get("year") or datetime.now().year)
        except ValueError:
            year = datetime.now().year
        pool = await self.pool()
        async with pool.acquire() as conn:
            result = await tax.estimate(conn, year)
            slips = await conn.fetch(
                """SELECT s.period, s.pay_date, p.name AS person, s.employer, s.tax_table,
                          s.tax_percent, s.gross_ytd_minor / 100.0 AS gross_ytd,
                          s.withheld_ytd_minor / 100.0 AS withheld_ytd,
                          s.net_paid_minor / 100.0 AS net_paid
                   FROM payslip s JOIN person p ON p.id = s.person_id
                   WHERE extract(year FROM s.period) = $1
                   ORDER BY p.name, s.period DESC""", year)
            settings_ = {r["key"]: r["value"] for r in await conn.fetch(
                """SELECT key, value FROM app_setting
                   WHERE key IN ('vat_registered_from', 'loan_interest_person', 'loan_interest_share')""")}
        rows = [("Salary and benefits", "salary"), ("Business profit (excl. VAT)", "business_profit"),
                ("Dividends", "dividends"), ("Minimum deduction", "minimum_deduction"),
                ("Pension contributions", "pension_deduction"),
                ("Loan interest", "interest_deduction"), ("Gifts, fees", "other_deduction"),
                ("General income", "general_income"),
                ("Tax on general income", "general_tax"), ("Bracket tax", "bracket_tax"),
                ("Social security", "social_security"), ("Total tax", "total_tax"),
                ("Withheld (projected to year end)", "withheld"), ("Advance tax paid", "prepaid")]
        # A household column when there is more than one person: each row summed
        people = result.get("people") or []
        household = ({key: sum(p[key] for p in people) for _, key in rows + [("", "balance")]}
                     if len(people) > 1 else None)
        return self.render(request, "tax.html", year=year, result=result, rows=rows,
                           slips=slips, settings=settings_, household=household)

    async def points_page(self, request: Request) -> Response:
        try:
            year = int(request.query_params.get("year") or datetime.now().year)
        except ValueError:
            year = datetime.now().year
        pool = await self.pool()
        async with pool.acquire() as conn:
            sources = await conn.fetch(
                """SELECT CASE WHEN partner ~ ' - ' THEN 'Flights flown' ELSE partner END AS source,
                          sum(bonus_points) AS earned, sum(level_points) AS level
                   FROM eb_txn WHERE extract(year FROM day) = $1 AND flow IN ('earned', 'level')
                   GROUP BY 1 ORDER BY sum(bonus_points) DESC NULLS LAST, 1""", year)
            spending = await conn.fetchrow(
                """SELECT count(*) FILTER (WHERE flow = 'award') AS kept_n,
                          coalesce(-sum(bonus_points) FILTER (WHERE flow = 'award'), 0) AS kept,
                          count(*) FILTER (WHERE flow = 'award_changed') AS changed_n,
                          coalesce(-sum(bonus_points) FILTER (WHERE flow = 'award_changed'), 0) AS changed,
                          coalesce(-sum(bonus_points) FILTER (WHERE flow = 'shop'), 0) AS shop,
                          coalesce(-sum(bonus_points) FILTER (WHERE flow = 'spent'), 0) AS other
                   FROM eb_txn WHERE extract(year FROM day) = $1""", year)
            awards = await conn.fetch(
                """SELECT day, partner, -bonus_points AS points FROM eb_txn
                   WHERE flow = 'award' AND extract(year FROM day) = $1 ORDER BY day""", year)
            level12 = await conn.fetchval("SELECT coalesce(sum(level_points), 0) FROM eb_txn "
                                          "WHERE day > current_date - interval '12 months'")
            newest = await conn.fetchval("SELECT max(day) FROM eb_txn")
            cards = await conn.fetch("SELECT code, label FROM points_card ORDER BY code")
            monthly = await conn.fetch(
                """SELECT * FROM report.points_monthly WHERE extract(year FROM period) = $1
                   ORDER BY card, period""", year)
            missed = await conn.fetch(
                """SELECT merchant, card, best_card, count(*) AS purchases, sum(-amount) AS amount,
                          round(sum(missed)) AS missed
                   FROM report.points_purchases WHERE extract(year FROM booking_date) = $1 AND missed > 0
                   GROUP BY merchant, card, best_card ORDER BY sum(missed) DESC LIMIT 20""", year)
            excluded = await conn.fetch(
                """SELECT excluded, count(*) AS purchases, sum(-amount) AS amount
                   FROM report.points_purchases WHERE extract(year FROM booking_date) = $1
                     AND excluded IS NOT NULL GROUP BY 1 ORDER BY 3 DESC""", year)
            rules = await conn.fetch("SELECT * FROM earn_rule ORDER BY card, valid_from")
            today = datetime.now(ZoneInfo(settings.timezone)).date()
            level = await progress.level_progress(
                conn, today, None if year == today.year else year)
            card_goals = [(c, await progress.card_progress(conn, c["code"], year, today)) for c in cards]
            earned = await progress.earned_monthly(conn, 24, today)
        by_card = {c["code"]: [m for m in monthly if m["card"] == c["code"]] for c in cards}
        totals = {code: {k: sum(m[k] for m in rows) for k in ("earning_spend", "expected", "received",
                                                                "difference", "missed")}
                  for code, rows in by_card.items()}
        return self.render(request, "points.html", year=year, sources=sources, level12=level12,
                           newest=newest, cards=cards, by_card=by_card, totals=totals, missed=missed,
                           spending=spending, awards=awards,
                           excluded=excluded, rules=rules,
                           labels={c["code"]: c["label"] for c in cards},
                           level_year=sum(s["level"] or 0 for s in sources),
                           missed_total=sum(m["missed"] for m in missed),
                           charts=_points_charts(level, card_goals, earned))

    async def trumf_page(self, request: Request) -> Response:
        try:
            months = max(1, min(60, int(request.query_params.get("months") or 12)))
        except ValueError:
            months = 12
        pool = await self.pool()
        async with pool.acquire() as conn:
            session = await conn.fetchrow(
                "SELECT updated_at, last_ok_at, last_error FROM trumf_session WHERE id = 1")
            since = await conn.fetchval(
                "SELECT (date_trunc('month', current_date) - make_interval(months => $1 - 1))::date",
                months)
            monthly = await conn.fetch(
                """SELECT date_trunc('month', day)::date AS month, count(*) AS purchases,
                          sum(amount) AS amount, sum(bonus) AS bonus, sum(savings) AS savings
                   FROM report.trumf_purchases WHERE category = 'ACCRUAL' AND grocery AND day >= $1
                   GROUP BY 1 ORDER BY 1 DESC""", since)
            chains = await conn.fetch(
                """SELECT partner_name AS partner, grocery, count(*) AS purchases, sum(amount) AS amount,
                          sum(bonus) AS bonus, round(avg(amount), 2) AS average
                   FROM report.trumf_purchases WHERE category = 'ACCRUAL' AND day >= $1
                   GROUP BY 1, 2 ORDER BY grocery DESC, sum(amount) DESC""", since)
            products = await conn.fetch(
                """SELECT product, count(DISTINCT batch_id) AS purchases, sum(quantity) AS quantity,
                          min(unit) AS unit, sum(amount) AS amount, sum(bonus) AS bonus
                   FROM report.trumf_lines WHERE grocery AND day >= $1 AND amount > 0
                   GROUP BY product ORDER BY sum(amount) DESC LIMIT 40""", since)
            used = await conn.fetchval(
                """SELECT coalesce(-sum(bonus), 0) FROM report.trumf_purchases
                   WHERE category = 'CONSUME' AND day >= $1""", since)
            counts = await conn.fetchrow(
                """SELECT count(*) FILTER (WHERE has_receipt) AS with_receipt,
                          count(*) FILTER (WHERE has_receipt AND lines = 0) AS missing,
                          count(*) FILTER (WHERE category = 'ACCRUAL' AND txn_id IS NULL) AS unmatched,
                          count(*) FILTER (WHERE category = 'ACCRUAL') AS purchases,
                          min(day) AS first, max(day) AS last
                   FROM report.trumf_purchases""")
        return self.render(request, "trumf.html", months=months, session=session, monthly=monthly,
                           chains=chains, products=products, used=used, counts=counts)

    async def business_page(self, request: Request) -> Response:
        from .. import business
        try:
            year = int(request.query_params.get("year") or datetime.now().year)
        except ValueError:
            year = datetime.now().year
        q = (request.query_params.get("q") or "").strip()[:100]
        pool = await self.pool()
        async with pool.acquire() as conn:
            terms = await business.terms(conn)
            expenses = await conn.fetch(
                """SELECT * FROM report.business_expenses WHERE extract(year FROM booking_date) = $1
                   ORDER BY booking_date DESC, id DESC""", year)
            rules = await conn.fetch(
                """SELECT r.*, (SELECT count(*) FROM business_expense e WHERE e.rule_id = r.id) AS n
                   FROM business_rule r ORDER BY r.id""")
            found = []
            if q:
                found = await conn.fetch(
                    """SELECT t.id, t.booking_date, a.name AS account, t.amount_minor / 100.0 AS amount,
                              coalesce(nullif(t.counterparty, ''), t.description) AS payee,
                              t.description, e.txn_id IS NOT NULL AS tagged
                       FROM txn t JOIN account a ON a.id = t.account_id
                       LEFT JOIN txn_class c ON c.txn_id = t.id
                       LEFT JOIN business_expense e ON e.txn_id = t.id
                       WHERE c.kind IS NULL AND t.booking_date >= current_date - interval '2 years'
                         AND (t.description ILIKE '%' || $1 || '%' OR t.counterparty ILIKE '%' || $1 || '%')
                       ORDER BY t.booking_date DESC, t.id DESC LIMIT 50""", q)
            registered = await db.get_setting(conn, "vat_registered_from")
        totals = {"cost": sum(e["cost"] for e in expenses), "vat": sum(e["input_vat"] for e in expenses),
                  "excl": sum(e["cost_excl_vat"] for e in expenses)}
        return self.render(request, "business.html", year=year, q=q, terms=terms, expenses=expenses,
                           rules=rules, found=found, totals=totals, rates=business.RATES,
                           registered=registered)

    async def business_post(self, request: Request) -> Response:
        from .. import business
        data = await self.form(request)
        action = data.get("action")
        back = data.get("back") or "/business"
        if not back.startswith("/business"):
            back = "/business"
        pool = await self.pool()
        async with pool.acquire() as conn:
            try:
                if action == "tag":
                    share = float(data.get("share") or 100) / 100
                    vat = (data.get("vat") or "").replace(",", ".").strip()
                    msg = await business.tag(conn, int(data["txn"]), share,
                                             float(data.get("vat_rate") or 25),
                                             float(vat) if vat else None,
                                             (data.get("note") or "").strip()[:200])
                    self.flash(request, "ok", f"Tagged: {msg.split(': ')[0]}.")
                elif action == "untag":
                    self.flash(request, "ok", (await business.untag(conn, int(data["txn"]))).capitalize() + ".")
                elif action == "rule":
                    rid = await business.add_rule(conn, (data.get("pattern") or "").strip(),
                                                  float(data.get("share") or 100) / 100,
                                                  float(data.get("vat_rate") or 25),
                                                  (data.get("note") or "").strip()[:200])
                    n = await conn.fetchval("SELECT count(*) FROM business_expense WHERE rule_id = $1", rid)
                    self.flash(request, "ok", f"Rule added: {n} payments tagged.")
                elif action == "delete-rule":
                    await business.delete_rule(conn, int(data["rule"]))
                    self.flash(request, "ok", "Rule deleted, with its tags.")
            except (ValueError, KeyError) as exc:
                self.flash(request, "error", f"Not saved: {exc}.")
        return RedirectResponse(back, status_code=303)

    async def categories_page(self, request: Request) -> Response:
        try:
            year = int(request.query_params.get("year") or datetime.now().year)
        except ValueError:
            year = datetime.now().year
        pool = await self.pool()
        async with pool.acquire() as conn:
            totals = await conn.fetch(
                """SELECT category, sum(spending) AS spending, sum(transactions) AS n
                   FROM report.category_monthly WHERE extract(year FROM month) = $1
                   GROUP BY category, sort_order ORDER BY sum(spending) DESC""", year)
            rules = await conn.fetch(
                """SELECT r.id, r.pattern, r.priority, c.name AS category,
                          (SELECT count(*) FROM txn_category tc WHERE tc.rule_id = r.id) AS matches
                   FROM category_rule r JOIN category c ON c.id = r.category_id
                   ORDER BY r.priority, r.id""")
            names = [r["name"] for r in await conn.fetch(
                "SELECT name FROM category ORDER BY sort_order, name")]
            other = await conn.fetch(
                """SELECT coalesce(t.counterparty, split_part(t.description, ',', 1)) AS payee,
                          count(*) AS n, round(-sum(t.amount_minor) / 100.0, 2) AS spending
                   FROM txn_category tc JOIN txn t ON t.id = tc.txn_id
                   JOIN category c ON c.id = tc.category_id
                   WHERE c.name = $1 AND extract(year FROM t.booking_date) = $2
                   GROUP BY 1 ORDER BY 3 DESC LIMIT 30""", categories.OTHER, year)
        total = sum(t["spending"] or 0 for t in totals)
        suggestions = [dict(o, pattern=re.escape(" ".join(str(o["payee"]).lower().split())[:60]))
                       for o in other]
        return self.render(request, "categories.html", year=year, totals=totals, total=total,
                           rules=rules, names=names, other=suggestions, other_name=categories.OTHER)

    async def category_items(self, request: Request) -> Response:
        """One category's payments: by month, by payee, and each item."""
        q = request.query_params
        name = (q.get("name") or "").strip()
        try:
            year = int(q.get("year") or datetime.now().year)
            month = int(q["month"]) if q.get("month") else None
        except ValueError:
            year, month = datetime.now().year, None
        if month is not None and not 1 <= month <= 12:
            month = None
        payee = (q.get("payee") or "").strip() or None
        start = date(year, month or 1, 1)
        end = (date(year + (month == 12), month % 12 + 1, 1) if month else date(year + 1, 1, 1)) \
            - timedelta(days=1)
        pool = await self.pool()
        async with pool.acquire() as conn:
            cid = await conn.fetchval("SELECT id FROM category WHERE name = $1", name)
            if cid is None:
                return PlainTextResponse("no such category", status_code=404)
            # The same rows as the category totals: categorised payments, and the
            # reimbursed parts of deposits (an expense paid back lowers its category)
            items_sql = """
                SELECT t.id, t.booking_date, coalesce(a.label, a.name, a.product, 'account ' || a.id)
                           AS account, t.counterparty, t.description, t.card_holder,
                       coalesce(t.counterparty, split_part(t.description, ',', 1)) AS payee,
                       round(-t.amount_minor / 100.0, 2) AS spending, t.original_amount,
                       t.original_currency, r.pattern, false AS reimbursed
                FROM txn_category tc JOIN txn t ON t.id = tc.txn_id
                JOIN account a ON a.id = t.account_id
                LEFT JOIN category_rule r ON r.id = tc.rule_id
                WHERE tc.category_id = $1 AND t.booking_date BETWEEN $2 AND $3
                UNION ALL
                SELECT t.id, t.booking_date, coalesce(a.label, a.name, a.product, 'account ' || a.id),
                       t.counterparty, t.description, t.card_holder,
                       coalesce(t.counterparty, split_part(t.description, ',', 1)),
                       round(-p.amount_minor / 100.0, 2), NULL, NULL, NULL, true
                FROM txn_part p JOIN txn t ON t.id = p.txn_id
                JOIN account a ON a.id = t.account_id
                LEFT JOIN txn_class c ON c.txn_id = t.id
                WHERE p.kind = 'reimbursement' AND c.kind IS NULL AND p.category_id = $1
                  AND t.booking_date BETWEEN $2 AND $3"""
            year_start, year_end = date(year, 1, 1), date(year, 12, 31)
            months = await conn.fetch(
                f"""SELECT extract(month FROM booking_date)::int AS month, sum(spending) AS spending,
                           count(*) AS n FROM ({items_sql}) i
                    WHERE $4::text IS NULL OR payee = $4
                    GROUP BY 1 ORDER BY 1""", cid, year_start, year_end, payee)
            payees = await conn.fetch(
                f"""SELECT payee, sum(spending) AS spending, count(*) AS n FROM ({items_sql}) i
                    GROUP BY 1 ORDER BY 2 DESC LIMIT 15""", cid, start, end)
            items = await conn.fetch(
                f"""SELECT * FROM ({items_sql}) i WHERE $4::text IS NULL OR payee = $4
                    ORDER BY booking_date DESC, id DESC LIMIT 2000""", cid, start, end, payee)
        by_month = {m["month"]: m for m in months}
        return self.render(request, "category.html", category=name, year=year, month=month,
                           payee=payee, items=items, payees=payees,
                           months=[(m, by_month.get(m)) for m in range(1, 13)],
                           total=sum(i["spending"] for i in items), truncated=len(items) == 2000)

    async def category_rule_post(self, request: Request) -> Response:
        data = await self.form(request)
        rid = request.path_params.get("rid")
        pool = await self.pool()
        async with pool.acquire() as conn:
            if rid is not None and data.get("action") == "delete":
                await conn.execute("DELETE FROM category_rule WHERE id = $1", rid)
                self.flash(request, "ok", "Rule deleted. Categories recomputed.")
            else:
                pattern = (data.get("pattern") or "").strip()
                name = clean_label(data.get("category"))
                try:
                    priority = max(1, min(10_000, int(data.get("priority") or 100)))
                except ValueError:
                    priority = 100
                problem = categories.check_pattern(pattern) or (None if name else
                                                                "a rule needs a category")
                if problem:
                    self.flash(request, "error", f"Not saved: {problem}.")
                    return RedirectResponse("/categories#rules", status_code=303)
                cid = await conn.fetchval(
                    """INSERT INTO category (name) VALUES ($1)
                       ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id""", name)
                if rid is None:
                    await conn.execute(
                        "INSERT INTO category_rule (category_id, pattern, priority) VALUES ($1, $2, $3)",
                        cid, pattern, priority)
                else:
                    await conn.execute(
                        """UPDATE category_rule SET category_id = $2, pattern = $3, priority = $4
                           WHERE id = $1""", rid, cid, pattern, priority)
                self.flash(request, "ok", f"Rule saved for {name}. Categories recomputed.")
            await categories.run(conn)
        return RedirectResponse("/categories#rules", status_code=303)

    # -- card files

    async def upload_page(self, request: Request, results: list | None = None,
                          status_code: int = 200) -> Response:
        pool = await self.pool()
        async with pool.acquire() as conn:
            cards = await conn.fetch(
                """SELECT a.id, a.account, a.bank, a.transactions,
                          min(t.booking_date) AS first_date, a.last_booking_date
                   FROM report.accounts a
                   JOIN account acc ON acc.id = a.id AND acc.cash_account_type = 'CARD'
                   LEFT JOIN txn t ON t.account_id = a.id
                   GROUP BY a.id, a.account, a.bank, a.transactions, a.last_booking_date
                   ORDER BY a.account""")
            uploads = await conn.fetch(
                """SELECT b.started_at, r.filename, b.source, b.seen, b.added, b.error
                   FROM import_batch b JOIN raw_record r ON r.batch_id = b.id AND r.kind = 'file'
                   ORDER BY b.started_at DESC LIMIT 20""")
        return self.render(request, "upload.html", status_code, results=results or [],
                           cards=cards, uploads=uploads, max_files=UPLOAD_MAX_FILES)

    async def upload(self, request: Request) -> Response:
        if request.method == "GET":
            return await self.upload_page(request)
        try:
            if int(request.headers.get("content-length") or 0) > UPLOAD_MAX_BYTES:
                return await self.upload_page(request, [{"status": "rejected", "filename": "",
                    "reason": "too much at once"}], 413)
        except ValueError:
            return PlainTextResponse("bad request", 400)
        form = await request.form(max_files=UPLOAD_MAX_FILES, max_fields=4)
        try:
            sent, expected = str(form.get("csrf") or ""), request.session.get("csrf", "")
            if not expected or not secrets.compare_digest(sent.encode(), expected.encode()):
                raise PermissionError("form expired")
            files = [f for f in form.getlist("files") if hasattr(f, "read") and f.filename]
            results = []
            pool = await self.pool()
            async with pool.acquire() as conn:
                for f in files:
                    content = await f.read(UPLOAD_MAX_FILE_BYTES + 1)
                    name = pathlib.PurePath(f.filename).name[:200]
                    if len(content) > UPLOAD_MAX_FILE_BYTES:
                        results.append({"filename": name, "status": "rejected",
                                        "reason": "larger than a card export can be"})
                        continue
                    results.append(await fileimport.import_file(conn, name, content,
                                                                run_pairing=False))
                if any(r["status"] == "imported" for r in results):
                    await pairing.run(conn)
        finally:
            await form.close()
        if not results:
            results = [{"filename": "", "status": "rejected", "reason": "no file chosen"}]
        return await self.upload_page(request, results)

    # -- BankID consent through Enable Banking

    async def eb_start(self, request: Request) -> Response:
        short = request.query_params.get("bank", "")
        if short not in settings.banks:
            return self.render(request, "message.html", 400, title="Unknown bank",
                               text="That bank is not configured (BANKS in .env).")
        name, country = settings.banks[short]
        who = clean_label(request.query_params.get("who"))
        state = secrets.token_urlsafe(24)
        valid_until = datetime.now(timezone.utc) + timedelta(days=settings.consent_days)
        try:
            url = await asyncio.to_thread(
                self.eb().start_auth, name, country, f"{self.public_url}/eb/callback", state,
                valid_until)
        except Exception as exc:
            log.warning("consent start for %s failed: %s", name, exc)
            return self.render(request, "message.html", 502, title="Could not start",
                               text=f"Enable Banking refused to start the consent: "
                                    f"{type(exc).__name__}.")
        request.session["eb"] = {"state": state, "bank": short, "who": who,
                                 "started": int(time.time())}
        log.info("consent flow started for %s%s", name, f" ({who})" if who else "")
        # Not a redirect: the Connect button is a form, and the page's
        # Content-Security-Policy (form-action 'self') makes browsers refuse
        # a form submission that redirects to another site. A page that
        # moves on by itself, with a link as fallback, is not a form action.
        return self.render(request, "to_bank.html", bank=name, who=who, url=url)

    async def eb_callback(self, request: Request) -> Response:
        flow = request.session.pop("eb", None)
        q = request.query_params
        if not flow or time.time() - flow.get("started", 0) > CONSENT_FLOW_SECONDS:
            return self.render(request, "message.html", 400, title="Start again",
                               text="The BankID flow took too long or was not started here.")
        if not secrets.compare_digest(q.get("state", "").encode(), flow["state"].encode()):
            return self.render(request, "message.html", 400, title="Start again",
                               text="The answer from the bank did not match this browser.")
        if q.get("error") or not q.get("code"):
            log.warning("consent refused at the bank: %s", q.get("error"))
            return self.render(request, "message.html", 400, title="Not authorised",
                               text="The bank did not grant access. Nothing was changed.")
        try:
            data = await asyncio.to_thread(self.eb().create_session, q["code"])
            pool = await self.pool()
            async with pool.acquire() as conn:
                consent_id = await sync.register_session(conn, self.eb(), data["session_id"], data,
                                                         label=flow.get("who"))
        except Exception as exc:
            log.exception("consent callback failed")
            return self.render(request, "message.html", 502, title="Could not finish",
                               text=f"The consent could not be stored: {type(exc).__name__}.")
        # History older than 90 days is readable for about an hour: fetch it now
        task = asyncio.create_task(self.full_import(consent_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        self.flash(request, "ok", "Consent stored. The full history is being fetched now; "
                                  "this page shows progress under Imports.")
        return RedirectResponse("/", status_code=303)

    async def full_import(self, consent_id: int) -> None:
        try:
            pool = await self.pool()
            async with pool.acquire() as conn:
                consent = await conn.fetchrow("SELECT * FROM consent WHERE id = $1", consent_id)
                result = await sync.sync_consent(conn, self.eb(), consent, full=True)
                await pairing.run(conn)
            log.info("full import: %s", result)
            if result["errors"]:
                notify.send(f"{result['bank']}: full import had problems",
                            "\n".join(result["errors"])[:1500])
        except Exception as exc:
            log.exception("full import failed")
            notify.send("full import failed", type(exc).__name__)

    # -- Claude connector

    async def mcp_endpoint(self, request: Request) -> Response:
        if not self.mcp_token:
            return PlainTextResponse("not found", 404)
        problem = bearer_problem(request.headers.get("authorization"), self.mcp_token)
        if problem:
            log.warning("mcp: refused, %s", problem)
            return JSONResponse({"error": "unauthorized"}, 401,
                                headers={"WWW-Authenticate": 'Bearer realm="heimkunnskap"'})
        origin = request.headers.get("origin")
        if origin is not None and origin != self.public_url:
            return PlainTextResponse("origin not allowed", 403)   # DNS rebinding guard
        if request.method != "POST":
            return PlainTextResponse("method not allowed", 405, headers={"Allow": "POST"})
        body = await read_limited(request, MCP_MAX_BYTES)
        if body is None:
            return JSONResponse({"error": "too large"}, 413)
        try:
            message = json.loads(body)
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "parse error"}}, 400)
        if isinstance(message, list):
            replies = [r for r in [await self.mcp.handle(m) for m in message] if r is not None]
            return JSONResponse(replies) if replies else Response(status_code=202)
        reply = await self.mcp.handle(message)
        return JSONResponse(reply) if reply is not None else Response(status_code=202)


class Gate(BaseHTTPMiddleware):
    """Access rules for every request, plus security headers."""

    def __init__(self, app, web: Web) -> None:
        super().__init__(app)
        self.web = web

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path == "/mcp":
            return _headers(await self.web.mcp_endpoint(request))
        if path == "/healthz":
            return _headers(PlainTextResponse("ok"))
        if not self.web.allowed(request):
            log.warning("refused %s from %s", path, self.web.client_ip(request))
            return _headers(PlainTextResponse("not available from this network", 403))
        if request.method not in ("GET", "HEAD") and not _same_origin(request):
            return _headers(PlainTextResponse("cross-site request refused", 403))
        try:
            response = await call_next(request)
        except PermissionError:
            self.web.flash(request, "error", "That form had expired. Please try again.")
            response = RedirectResponse("/", status_code=303)
        return _headers(response)


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True
    host = request.headers.get("host", "")
    return origin in (f"http://{host}", f"https://{host}")


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def bearer_problem(header: str | None, token: str) -> str | None:
    """None if the Authorization header carries the token, else what is
    wrong with it, for the log: never the header itself, only its shape, its
    length and a fingerprint of the token sent (sha256, first 12 hex), to
    set against: grep '^MCP_TOKEN=' .env | cut -d= -f2- | tr -d '\\n' | sha256sum
    The scheme is case-insensitive and the bare token is accepted too, since
    connector header fields invite pasting the token alone (docs/mcp-pattern.md)."""
    if header is None:
        return "no Authorization header"
    scheme, _, rest = header.strip().partition(" ")
    sent = rest.strip() if scheme.lower() == "bearer" else header.strip()
    if secrets.compare_digest(sent.encode(), token.encode()):
        return None
    if not sent:
        return "'Bearer' without a token"
    shape = []
    if any(c.isspace() for c in sent):
        shape.append("whitespace inside the token")
    if len(sent) != len(token):
        shape.append(f"{len(sent)} characters, expected {len(token)}")
    return (f"wrong token ({', '.join(shape) or 'same length'}; "
            f"sent sha256 {_fingerprint(sent)}, expected {_fingerprint(token)})")


def _headers(response: Response) -> Response:
    h = response.headers
    h["Content-Security-Policy"] = ("default-src 'none'; style-src 'self'; img-src 'self' data:; "
                                    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "same-origin"
    if "cache-control" not in h:
        h["Cache-Control"] = "no-store"
    return response


def clean_label(value) -> str | None:
    """A short free-text name: whose consent, or an account's own name."""
    text = " ".join(str(value or "").split())[:40]
    return text or None


def fmt_kr(value) -> str:
    if value is None:
        return ""
    s = f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
    return s.replace("-", "−")


def _points_charts(level: dict, card_goals: list, earned: dict) -> dict:
    """The Points page's progress charts (web/charts.py)."""
    order = progress.SOURCE_ORDER
    out = {"level": dict(level, svg=charts.progress(
               level["series"], level["start"], level["end"], level["today"], level["milestones"],
               "points", level["projected"], label="Level points in the qualification period")),
           "cards": [], "earned": None}
    for card, goal in card_goals:
        if goal:
            out["cards"].append(dict(goal, card=card, svg=charts.progress(
                goal["series"], goal["start"], goal["end"], goal["today"], goal["milestones"], "kr",
                goal["projected"], goal["cap"], label=f"{card['label']}: spending this year")))
    if earned["stacks"]:
        present = [s for s in order if s in earned["stacks"]]
        out["earned"] = {
            "svg": charts.stacked_months(earned["months"], earned["stacks"], order,
                                         label="EuroBonus points earned per month",
                                         names=earned["labels"]),
            "legend": charts.legend([(order.index(s) + 1, earned["labels"][s]) for s in present]),
            "rows": [(earned["labels"][s], sum(earned["stacks"][s])) for s in present],
            "months": earned["months"]}
    return out


def fmt_kr0(value) -> str:
    """Whole kroner, for estimates."""
    if value is None:
        return ""
    return f"{round(float(value)):,}".replace(",", " ").replace("-", "−")


def fmt_when(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        from zoneinfo import ZoneInfo
        return value.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M")
    return str(value)


def secret_key() -> str:
    if len(settings.web_secret_key) >= 32:
        return settings.web_secret_key
    log.warning("WEB_SECRET_KEY is missing or short: using a random one, so a BankID flow "
                "in progress is lost on restart")
    return secrets.token_urlsafe(48)


def create_app(secret: str | None = None, **kwargs) -> Starlette:
    web = Web(**kwargs)
    routes = [
        Route("/", web.home),
        Route("/accounts/{aid:int}/label", web.label_post, methods=["POST"]),
        Route("/consents/{cid:int}/label", web.consent_label_post, methods=["POST"]),
        Route("/eb/start", web.eb_start),
        Route("/eb/callback", web.eb_callback),
        Route("/upload", web.upload, methods=["GET", "POST"]),
        Route("/categories", web.categories_page),
        Route("/category", web.category_items),
        Route("/tax", web.tax_page),
        Route("/points", web.points_page),
        Route("/groceries", web.trumf_page),
        Route("/business", web.business_page),
        Route("/business", web.business_post, methods=["POST"]),
        Route("/categories/rules", web.category_rule_post, methods=["POST"]),
        Route("/categories/rules/{rid:int}", web.category_rule_post, methods=["POST"]),
        Route("/mcp", lambda r: PlainTextResponse("not found", 404),
              methods=["GET", "POST", "DELETE"]),
        # Browsers ask for /favicon.ico whatever the page links to
        Route("/favicon.ico", lambda r: FileResponse(HERE / "static" / "coin-32.png",
                                                      media_type="image/png")),
        Mount("/static", StaticFiles(directory=str(HERE / "static")), name="static"),
    ]
    app = Starlette(routes=routes, middleware=[
        Middleware(SessionMiddleware, secret_key=secret or secret_key(),
                   session_cookie="heimkunnskap", max_age=12 * 3600, same_site="lax",
                   https_only=web.public_url.startswith("https://")),
        Middleware(Gate, web=web),
    ])
    app.state.web = web
    return app
