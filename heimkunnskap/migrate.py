"""Minimal forward-only migration runner, applies
sql/NNN_*.sql in lexical order and records each in schema_migrations.
Then re-applies the read-only grants, so new report views are covered."""

from __future__ import annotations

import asyncio
import logging
import pathlib

from .config import settings

log = logging.getLogger("heimkunnskap.migrate")
SQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "sql"

# The read-only login (<owner>_ro, Grafana) sees the report schema and
# nothing else. The MCP login (<owner>_mcp) sees only the report views named
# in MCP_VIEWS, so what leaves the house through Claude is a setting, and
# whatever is not listed is refused by the database itself.
READONLY_GRANTS = """
DO $$
DECLARE ro text := current_user || '_ro';
BEGIN
    REVOKE ALL ON SCHEMA public FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = ro) THEN
        EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', ro);
        EXECUTE format('REVOKE ALL ON SCHEMA public FROM %I', ro);
        EXECUTE format('GRANT USAGE ON SCHEMA report TO %I', ro);
        EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA report TO %I', ro);
        EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA report GRANT SELECT ON TABLES TO %I', ro);
    END IF;
END $$;
"""

MCP_GRANTS = """
SELECT format('%I', current_user || '_mcp') AS role,
       EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user || '_mcp') AS present
"""


async def apply_mcp_grants(conn, views: tuple) -> list[str]:
    """Grant the MCP login SELECT on exactly these report views. Returns
    the views granted; unknown names are skipped with a warning."""
    row = await conn.fetchrow(MCP_GRANTS)
    if not row["present"]:
        log.warning("no MCP login role: the Claude connector cannot read anything")
        return []
    role = row["role"]
    existing = {r["table_name"] for r in await conn.fetch(
        "SELECT table_name FROM information_schema.views WHERE table_schema = 'report'")}
    await conn.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}")
    await conn.execute(f"REVOKE ALL ON SCHEMA public FROM {role}")
    await conn.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA report FROM {role}")
    await conn.execute(f"GRANT USAGE ON SCHEMA report TO {role}")
    if "*" in views:                    # MCP_VIEWS=*: every report view, present and future
        views = tuple(sorted(existing))
    granted = []
    for view in views:
        if view not in existing:
            log.warning("MCP_VIEWS names %r, which is not a report view", view)
            continue
        await conn.execute(f'GRANT SELECT ON report."{view}" TO {role}')
        granted.append(view)
    log.info("MCP login may read: %s", ", ".join(granted) or "nothing")
    return granted


async def run() -> None:
    import asyncpg

    conn = await asyncpg.connect(settings.database_url)
    try:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                 filename text PRIMARY KEY, applied_at timestamptz DEFAULT now())""")
        applied = {r["filename"] for r in await conn.fetch(
            "SELECT filename FROM schema_migrations")}
        for path in sorted(SQL_DIR.glob("0*.sql")):
            if path.name in applied:
                continue
            log.info("applying %s", path.name)
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES ($1)", path.name)
        ro = await conn.fetchval("SELECT current_user || '_ro'")
        exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)", ro)
        await conn.execute(READONLY_GRANTS)
        if exists:
            log.info("read-only grants ensured for %s", ro)
        else:
            log.warning("role %s does not exist: no read-only login for Grafana", ro)
        await apply_mcp_grants(conn, settings.mcp_views)
    finally:
        await conn.close()


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run())
