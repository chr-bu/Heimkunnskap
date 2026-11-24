"""Claude connector: a read-only MCP server (Streamable HTTP, JSON responses)
at <web app>/mcp (docs/mcp-pattern.md).

Off unless MCP_TOKEN is set. Every request needs the token as a bearer
token; Apache admits the MCP path from Anthropic's address range only.
The tools read through the MCP database login, which sees only the report
views named in MCP_VIEWS (heimkunnskap/migrate.py): the database, not this
code, decides what can leave the house. Nothing here can change data or
reach a bank.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from .. import __version__
from ..config import settings

log = logging.getLogger("heimkunnskap.web.mcp")

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_ROWS = 500

INSTRUCTIONS = (
    "Heimkunnskap is one household's finance ledger: bank accounts read nightly from DNB "
    "and the SAS EuroBonus Mastercard through Enable Banking (BankID consents), in NOK. "
    "Card exports (American Express, DNB Mastercard) are uploaded monthly. "
    "Start with finance_report for income and spending per month; sync_status for whether "
    "the data is current; tax_estimate for the year's income tax per person; sql_query for "
    "anything else. Moves between the household's own "
    "accounts and card bill payments are classified and left out of income and spending "
    "(household_monthly); review lists moves whose other side was not found. Only some "
    "report views are shared; a 'permission denied' means that view is not shared with Claude.")

SCHEMA_HINT = (
    "Schema report (the search path). Views, when shared: "
    "household_monthly (month, income, spending, saving, loans, net, unmatched_moves, "
    "transactions, interest, card_bills_no_detail: the household as a whole, without moves between own accounts "
    "or card bill payments; a card refund reduces spending; a card bill whose card has no data for the "
    "period counts as spending (card_bills_no_detail, category Card purchases (no detail)); loans is principal repaid, "
    "negative when drawn from the credit-line mortgage (rammelån); interest is loan "
    "interest and fees; money the household lent out counts as saving when lent and "
    "negative saving when repaid, and only the interest on it is income); "
    "monthly (month, account_id, account, money_in, money_out, net, transactions, transfers: "
    "per account, moves shown apart in transfers); "
    "transactions (id, account_id, account, booking_date, amount, currency, original_amount, "
    "original_currency, exchange_rate, description, counterparty, card_holder, kind, "
    "partner_id, rule, extra, loan_principal, loan_interest, loan_fees, category; kind is own_transfer, "
    "card_payment, saving, loan, reimbursement (an expense paid back, which reduces "
    "spending; partner_id is the expense), lending (money lent out, and its repayments with "
    "partner_id the loan; loan_principal and loan_interest split each repayment) or null "
    "for ordinary income and spending; rule manual:* is a mark made by hand); "
    "lending (one row per loan the household made: lent_on, lent, repaid, interest, "
    "outstanding, last_repayment, note); "
    "review (moves with no partner found); "
    "tax_estimate (year, as_of, person, salary, business_profit, dividends, minimum_deduction, "
    "pension_deduction, interest_deduction, general_income, general_tax, bracket_tax, "
    "social_security, total_tax, withheld, prepaid, balance (positive: refund, negative: "
    "to pay), marginal_rate_salary, marginal_rate_business, notes: the latest estimate, "
    "salary and withholding projected to year end from the newest pay slips); "
    "payslips (period, pay_date, person, employer, gross_ytd, withheld_ytd, pension_ytd, "
    "salary, benefits, withholding, tax_table, tax_percent, net_paid: one row per pay slip); "
    "expense_claims (id, claim_no, person, title, purpose, trip_from, trip_to, total, tax_free, "
    "taxable, outlays, lines_matched, lines_ambiguous, paid_on, paid: travel expense claims; "
    "the deposit that paid one is split: outlays paid back reduce spending in the category of "
    "the card payment, the rest is income); expense_claim_lines (id, claim_no, line_no, section "
    "(allowance or cost), kind, day, type_text, note, amount, currency, currency_amount, match, "
    "txn_id, paid_on, paid, paid_text: each claimed item and the card payment it was); "
    "business_expenses (id, booking_date, account, description, amount, share, vat_rate, cost, "
    "input_vat, cost_excl_vat, source, rule, note: payments tagged as the sole proprietorship's; "
    "cost excl. VAT lowers its profit); vat_terms (term_start, term_end, due, sales_excl_vat, "
    "output_vat, input_vat, net, paid, outstanding: VAT per two-month term); "
    "eurobonus (id, day, partner, kind, bonus_points, level_points, activity: the EuroBonus "
    "account's history; negative bonus points are spent); points_monthly (card, period, "
    "earning_spend, expected, received, difference, purchases, missed: per card (amex, dnb, sas) "
    "and posting month); points_purchases (id, booking_date, card, period, merchant, amount, "
    "foreign_cur, excluded, points, best_card, best_points, missed, description: each card "
    "purchase's expected points and the best card's); "
    "trumf_purchases (batch_id, day, at, category: ACCRUAL a purchase, CONSUME bonus moved to "
    "EuroBonus; partner: chain (KIWI, MENY, SPAR, JOK, VIAT web shops); store, amount, bonus "
    "(Trumf bonus kr), savings, has_receipt, lines, txn_id, paid_with: the card payment it "
    "matched), partner_name, grocery (a grocery store, not web shops, florist or books); trumf_lines (batch_id, day, partner, store, line_no, product, ean, quantity, "
    "unit: EA or KG, amount, bonus, bonus_pct, vat_pct, unknown_item, bonus_rules, grocery: one row "
    "per product on a receipt); "
    "category_monthly (month, category, sort_order, spending, transactions: spending per "
    "category, the same spending as household_monthly); category_rules (category, pattern, "
    "priority: how payees are sorted, first match by priority wins, else Other); "
    "accounts (id, account, bank, product, cash_account_type: CARD for cards, number_masked, "
    "is_business_income, last_booking_date, transactions); balances (latest per account and "
    "balance_type); consents (bank, label = whose BankID, status, valid_until, days_left, "
    "accounts, last_sync_at, last_error); imports (one row per sync or upload). "
    "Amounts are kroner; negative is money out.")

TOOLS = [
    {"name": "finance_report",
     "description": ("Money in, money out and net per month across all accounts for the last "
                     "months, the latest month per account, and the latest balances."),
     "inputSchema": {"type": "object", "properties": {
         "months": {"type": "integer", "minimum": 1, "maximum": 60, "default": 3,
                    "description": "How many months back, the current one included"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "sync_status",
     "description": ("Whether the data is current: each bank consent with days left and its "
                     "last sync or error, and the newest booking per account."),
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},
    {"name": "import_log",
     "description": "Recent import batches (nightly syncs, full imports) with counts and errors.",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
         "errors_only": {"type": "boolean", "default": False}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "tax_estimate",
     "description": ("The income tax estimate for a year, per person: income, deductions, each "
                     "tax, the tax withheld projected to year end, and the expected refund or "
                     "tax to pay, with notes on what is projected or left out; and the pay "
                     "slips behind it."),
     "inputSchema": {"type": "object", "properties": {
         "year": {"type": "integer", "minimum": 2020, "maximum": 2100,
                  "description": "Income year; default the current one"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "sql_query",
     "description": ("Run one read-only SQL statement (PostgreSQL 17) against the shared "
                     f"report views and get the rows as a table (at most {MAX_ROWS}; 15 s "
                     "limit; Europe/Oslo time). " + SCHEMA_HINT),
     "inputSchema": {"type": "object", "required": ["sql"], "properties": {
         "sql": {"type": "string", "description": "A single SELECT (or WITH ... SELECT)"}}},
     "annotations": {"readOnlyHint": True}},
]


class McpServer:
    def __init__(self, pool_getter) -> None:
        self._pool = pool_getter              # async () -> pool on the MCP login

    async def handle(self, message) -> dict | None:
        """One JSON-RPC message; None for notifications."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "invalid request")
        mid = message.get("id")
        method = message.get("method")
        if mid is None:                       # notification (e.g. notifications/initialized)
            return None
        params = message.get("params") or {}
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                return _result(mid, {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "heimkunnskap", "version": __version__},
                    "instructions": INSTRUCTIONS})
            if method == "ping":
                return _result(mid, {})
            if method == "tools/list":
                return _result(mid, {"tools": TOOLS})
            if method == "tools/call":
                text, is_error = await self.call(params.get("name"), params.get("arguments") or {})
                return _result(mid, {"content": [{"type": "text", "text": text}],
                                     "isError": is_error})
            return _error(mid, -32601, f"method not found: {method}")
        except Exception as exc:              # never leak a traceback to the client
            log.exception("mcp %s failed", method)
            return _error(mid, -32603, f"internal error: {type(exc).__name__}")

    async def _read(self, sql: str, *args) -> list:
        """One read-only query on the MCP login, with the same limits as sql_query."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                await conn.execute("SET LOCAL statement_timeout = '15s'")
                await conn.execute("SET LOCAL search_path = report")
                await conn.execute("SELECT set_config('TimeZone', $1, true)", settings.timezone)
                return await conn.fetch(sql, *args)

    async def _section(self, title: str, sql: str, *args) -> str:
        try:
            rows = await self._read(sql, *args)
        except Exception as exc:
            if "permission denied" in str(exc):
                return f"## {title}\n(not shared with Claude)"
            raise
        return f"## {title}\n{format_table(rows)}"

    async def call(self, name: str, args: dict) -> tuple[str, bool]:
        log.info("mcp tool %s %s", name, json.dumps(args, default=str)[:300])
        if name == "finance_report":
            months = _int(args.get("months"), 3, 1, 60)
            parts = [
                await self._section(
                    f"The household, last {months} months",
                    """SELECT month, income, spending, saving, loans, interest, net, unmatched_moves,
                              card_bills_no_detail
                       FROM household_monthly
                       WHERE month >= (date_trunc('month', now()) - make_interval(months => $1 - 1))::date
                       ORDER BY month""", months),
                await self._section(
                    "Latest month per account",
                    """SELECT account, month, money_in, money_out, net, transactions
                       FROM monthly WHERE month = (SELECT max(month) FROM monthly)
                       ORDER BY money_out DESC NULLS LAST"""),
                await self._section(
                    "Latest balances (booked, and available as the bank shows it)",
                    """SELECT account, account_id,
                              coalesce(max(amount) FILTER (WHERE balance_type = 'CLBD'),
                                       max(amount) FILTER (WHERE balance_type = 'OPBD')) AS booked,
                              coalesce(max(amount) FILTER (WHERE balance_type = 'XPCD'),
                                       max(amount) FILTER (WHERE balance_type = 'CLAV'),
                                       max(amount) FILTER (WHERE balance_type = 'ITAV')) AS available,
                              max(as_of) AS as_of
                       FROM balances GROUP BY account_id, account ORDER BY account, account_id"""),
            ]
            return "\n\n".join(parts), False
        if name == "sync_status":
            parts = [
                json.dumps({"heimkunnskap_version": __version__,
                            "now_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}),
                await self._section(
                    "Consents",
                    """SELECT bank, label AS whose, status, valid_until, days_left, accounts,
                              last_sync_at, last_error
                       FROM consents ORDER BY bank, valid_until DESC"""),
                await self._section(
                    "Accounts",
                    """SELECT account, bank, number_masked, last_booking_date, transactions
                       FROM accounts WHERE active ORDER BY bank, account"""),
            ]
            return "\n\n".join(parts), False
        if name == "import_log":
            limit = _int(args.get("limit"), 20, 1, 200)
            where = "WHERE error IS NOT NULL" if args.get("errors_only") else ""
            return await self._section(
                "Imports",
                f"""SELECT started_at, kind, account, date_from, pages, seen, added, error
                    FROM imports {where} ORDER BY started_at DESC LIMIT $1""", limit), False
        if name == "tax_estimate":
            year = _int(args.get("year"), date.today().year, 2020, 2100)
            parts = [
                await self._section(
                    f"Income tax {year}, kroner (balance: positive is a refund)",
                    """SELECT person, as_of, salary, business_profit, dividends, minimum_deduction,
                              pension_deduction, interest_deduction, general_income, general_tax,
                              bracket_tax, social_security, total_tax, withheld, prepaid, balance,
                              marginal_rate_salary, marginal_rate_business
                       FROM tax_estimate WHERE year = $1 ORDER BY person""", year),
                await self._section(
                    "Notes",
                    """SELECT person, jsonb_array_elements_text(notes) AS note
                       FROM tax_estimate WHERE year = $1 ORDER BY person""", year),
                await self._section(
                    "Pay slips",
                    """SELECT period, person, employer, gross_ytd, withheld_ytd, salary, withholding,
                              tax_table, net_paid
                       FROM payslips WHERE extract(year FROM period) = $1
                       ORDER BY person, period""", year),
            ]
            return "\n\n".join(parts), False
        if name == "sql_query":
            sql = str(args.get("sql") or "").strip().rstrip(";").strip()
            if not sql:
                return "No SQL given.", True
            try:
                rows = await self._read(sql)
            except Exception as exc:
                return f"{type(exc).__name__}: {exc}", True
            more = len(rows) > MAX_ROWS
            text = format_table(rows[:MAX_ROWS])
            return text + (f"\n(first {MAX_ROWS} of {len(rows)} rows)" if more else ""), False
        return f"Unknown tool: {name}", True


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="minutes")
    if isinstance(v, (date, Decimal)):
        return str(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str, ensure_ascii=False)
    return str(v).replace("\n", " ").replace("|", "\\|")


def format_table(rows) -> str:
    """Rows as a Markdown table."""
    if not rows:
        return "(no rows)"
    cols = list(rows[0].keys())
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        lines.append("| " + " | ".join(_cell(r[c]) for c in cols) + " |")
    return "\n".join(lines)


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _result(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
