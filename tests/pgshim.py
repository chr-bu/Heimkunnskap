"""Just enough of an asyncpg connection, over psql, to run heimkunnskap.db
against a real PostgreSQL where asyncpg is not installed.

Each statement is sent as PREPARE + EXECUTE, so PostgreSQL infers the
parameter types the same way it does for asyncpg. Results come back as
JSON and dates and timestamps are converted back by their shape. Each
call is its own psql session: session state (advisory locks) does not
carry over. transaction() is not atomic; it only replays SET LOCAL and
read-only mode for the calls inside it. Test use only."""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
from datetime import date, datetime
from decimal import Decimal

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2})$")


def literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, (date, datetime)):
        return "'" + value.isoformat() + "'"
    if isinstance(value, bytes):
        return "'\\x" + value.hex() + "'"
    return "'" + str(value).replace("'", "''") + "'"


def convert(value):
    if isinstance(value, str):
        if DATE.match(value):
            return date.fromisoformat(value)
        if STAMP.match(value):
            return datetime.fromisoformat(value)
    if isinstance(value, dict):
        return {k: convert(v) for k, v in value.items()}
    return value


class PsqlConn:
    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo
        self.prelude: list[str] | None = None     # session settings inside transaction()

    def _run(self, sql: str) -> str:
        prelude = "".join(s + ";\n" for s in self.prelude or [])
        if prelude:                                # settings only: discard their output
            prelude = "\\o /dev/null\n" + prelude + "\\o\n"
        proc = subprocess.run(
            ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", self.conninfo],
            input="SET client_min_messages = error;\nSET timezone = 'UTC';\n" + prelude + sql,
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip())
        return proc.stdout.strip()

    def _call(self, sql: str, args) -> list[dict]:
        sql = sql.strip().rstrip(";")
        modifies = re.match(r"(?is)^\s*(insert|update|delete|with)\b", sql)
        if modifies and re.search(r"(?i)\breturning\b", sql):
            # A data-modifying statement can only be wrapped as a CTE
            body = f"WITH r AS ({sql}) SELECT coalesce(json_agg(r), '[]') FROM r"
        elif re.match(r"(?is)^\s*select\b", sql):
            body = f"SELECT coalesce(json_agg(q), '[]') FROM ({sql}) q"
        elif not args:
            self._run(sql + ";\n")
            return []
        else:
            body = sql
        params = "(" + ", ".join(literal(a) for a in args) + ")" if args else ""
        out = self._run(f"PREPARE s AS {body};\nEXECUTE s{params};\n")
        if not out:
            return []
        return [convert(r) for r in json.loads(out)]

    async def execute(self, sql: str, *args) -> str:
        if self.prelude is not None and re.match(r"(?is)^\s*(set|select set_config)\b", sql):
            # SET LOCAL lasts for the transaction: here, for the calls inside it
            for i in range(len(args), 0, -1):
                sql = sql.replace(f"${i}", literal(args[i - 1]))
            sql = re.sub(r"(?i)^\s*set local\b", "SET", sql.strip())
            self.prelude.append(sql.replace(", true)", ", false)"))    # set_config: session
            return "SET"
        self._call(sql, args)
        return "OK"

    async def executemany(self, sql: str, rows) -> None:
        for args in rows:
            self._call(sql, args)

    async def fetch(self, sql: str, *args) -> list[dict]:
        return self._call(sql, args)

    async def fetchrow(self, sql: str, *args):
        rows = self._call(sql, args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args):
        row = await self.fetchrow(sql, *args)
        return next(iter(row.values())) if row else None

    def transaction(self, readonly: bool = False):
        return _Transaction(self, readonly)

    async def close(self) -> None:
        pass


class _Transaction(contextlib.AbstractAsyncContextManager):
    """No real transaction; collects session settings for the calls inside."""

    def __init__(self, conn: PsqlConn, readonly: bool) -> None:
        self.conn, self.readonly, self.outer = conn, readonly, None

    async def __aenter__(self):
        self.outer = self.conn.prelude
        self.conn.prelude = list(self.outer or []) + (
            ["SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"] if self.readonly else [])
        return self

    async def __aexit__(self, *exc):
        self.conn.prelude = self.outer
        return False


class PsqlPool:
    """asyncpg pool lookalike: acquire() hands out a fresh PsqlConn."""

    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    def acquire(self):
        conn = PsqlConn(self.conninfo)

        class _Acquire(contextlib.AbstractAsyncContextManager):
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Acquire()
