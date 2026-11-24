# The MCP server (Claude connector)

Heimkunnskap includes a small read-only MCP server so Claude can answer questions about the household's finances. This file records how it is built and secured.

## The pattern

**Hand-rolled, no SDK.** A small JSON-RPC handler serving MCP over Streamable HTTP with plain JSON responses (no event streams). It answers `initialize`, `ping`, `tools/list` and `tools/call`, accepts the protocol versions `2025-06-18`, `2025-03-26` and `2024-11-05`, and returns HTTP 202 for notifications. Batches are handled. It runs inside the web app (Starlette on uvicorn) at `<base path>/mcp`.

**Off unless configured.** Without `MCP_TOKEN` in `.env` the endpoint answers 404.

**Two locks.**

1. Apache admits only Anthropic's outbound range on the MCP path:

   ```apache
   <Location /mcp>
       Require ip 160.79.104.0/21
   </Location>
   ```

   The range is listed at https://platform.claude.com/docs/en/api/ip-addresses and should be checked there when deploying.
2. The app checks `Authorization: Bearer <MCP_TOKEN>` with a constant-time comparison. The scheme is case-insensitive, and the bare token without `Bearer` is accepted too, because connector header fields invite pasting the token alone.

**Diagnosable refusals.** A refused request is logged with what was wrong (no header, wrong length, whitespace inside) and the first 12 hex digits of the SHA-256 of the token sent and expected. The token itself is never logged. To compare on the server:

```sh
grep '^MCP_TOKEN=' .env | cut -d= -f2- | tr -d '\n' | sha256sum
```

**Other request checks.** POST only (405 otherwise), body capped at 256 kB (413), and an `Origin` header, when present, must match the app's own origin (DNS rebinding guard).

**Read-only, twice.** No tool writes. The tools connect with a separate read-only database login (below), and `sql_query` runs each statement inside a read-only transaction with `SET LOCAL statement_timeout = '15s'` and the local time zone, returning at most 500 rows.

**Errors stay small.** SQL errors come back as a tool error with the exception type and message. Any other failure returns `internal error: <type>` and the traceback goes only to the server log.

**Every call is logged** with the tool name and the first 300 characters of its arguments.

**Self-describing.** `initialize` returns short instructions, and the `sql_query` description carries a schema hint naming the main tables and views, so Claude can start without exploring.

## What Claude can reach

- **Views, not tables.** The MCP login has SELECT on reporting views only. Raw records, consents, the Trumf login, pay slip files and secrets are not reachable.
- **A login of its own.** The MCP server does not share Grafana's login. It connects as `<db user>_mcp`, which may read only the report views listed in `MCP_VIEWS`. Leaving `transactions` out shares totals only; adding it shares individual transactions and merchants; `*` shares every report view. The worker re-applies these grants on every start, so the database itself refuses anything not listed.
- **Only the MCP path is public.** The web app, Grafana and database stay on the LAN and VPN. The vhost therefore has a LAN-only rule for everything except the MCP path. LAN clients that use the public hostname arrive through the router with the home's public address, so that address belongs in the LAN rule too.
- **A public name has a side benefit.** Once the hostname has public DNS so Claude can reach the MCP path, Let's Encrypt can validate it over HTTP, with no DNS challenge needed for a LAN-only service.
- **Tools.** `finance_report`, `sync_status`, `import_log`, `sql_query` and `tax_estimate`.
- **Answers leave the house.** Whatever a tool returns is sent to Claude and becomes part of that conversation. Share only the views you are comfortable with.

## Stack

Starlette and uvicorn for the web app, asyncpg for Postgres, Jinja2 templates, and forward-only numbered SQL migrations (`sql/NNN_*.sql`) applied by a small runner that records them in a `schema_migrations` table. Outgoing calls to Enable Banking use the standard library's `urllib`, since there are few of them and they run in a worker thread.
