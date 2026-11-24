#!/bin/sh
# Runs once, on the first start of an empty Postgres volume: creates the
# database, its owner role (used by the sync and the web app), a read-only
# role for Grafana (<owner>_ro) and a narrower one for the MCP server
# (<owner>_mcp). Grants are applied on every start (heimkunnskap/migrate.py):
# _ro sees the report schema, _mcp only the report views in MCP_VIEWS.
# Names and passwords come from .env.
set -eu
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres \
     -v db="$HEIM_DB" -v owner="$HEIM_DB_USER" -v ro="${HEIM_DB_USER}_ro" \
     -v mcp="${HEIM_DB_USER}_mcp" \
     -v pass="$DB_PASS" -v ro_pass="$DB_PASS_RO" -v mcp_pass="$DB_PASS_MCP" <<'SQL'
CREATE ROLE :"owner" LOGIN PASSWORD :'pass';
CREATE DATABASE :"db" OWNER :"owner";
REVOKE CONNECT ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO :"owner";
CREATE ROLE :"ro" LOGIN PASSWORD :'ro_pass';
GRANT CONNECT ON DATABASE :"db" TO :"ro";
CREATE ROLE :"mcp" LOGIN PASSWORD :'mcp_pass';
GRANT CONNECT ON DATABASE :"db" TO :"mcp";
SQL
