# Security

Heimkunnskap holds a household's complete financial record, so security problems matter more than most bugs.

## Reporting a problem

Please report security problems privately, through GitHub's private vulnerability reporting: the repository's **Security** tab, **Report a vulnerability**. Do not open a public issue.

Include what an attacker could do, how to reproduce it, and the version (commit) you looked at. Never include real financial data or personal information, yours or anyone else's.

## What is in scope

- Anything that lets someone outside the household's LAN read or change data: the web app, the MCP endpoint, the BankID consent flow, the mail intake.
- The MCP server reaching more than the report views it is given, or writing anything.
- Parsers that can be made to run code, read files or exhaust the server through a crafted upload or email attachment.
- Secrets (the Enable Banking key, tokens, the Trumf session cookie) ending up in logs, report views, backups in clear text, or responses.

## How the app is meant to run

The design assumes the web app, database and Grafana are reachable only from the LAN and a VPN, with the MCP path the one public exception, limited to Anthropic's address range and a bearer token (`docs/design.md`, Security; `docs/mcp-pattern.md`). A report that depends on exposing the whole app to the internet is still welcome, but say so.
