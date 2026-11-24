"""Database access for the writer (worker and web). Plain SQL over asyncpg.

The report schema is for readers; this module writes the public tables."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime

from .config import settings


async def connect():
    import asyncpg
    return await asyncpg.connect(settings.database_url, server_settings={
        "timezone": settings.timezone, "application_name": "heimkunnskap"})


async def create_pool(url: str | None = None, **kw):
    import asyncpg
    return await asyncpg.create_pool(url or settings.database_url, min_size=1, max_size=4,
                                     server_settings={"timezone": settings.timezone,
                                                      "application_name": "heimkunnskap"}, **kw)


def _date(value) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


# -- consents

async def save_consent(conn, session_id: str, aspsp_name: str, country: str,
                       valid_until: datetime, label: str | None = None) -> int:
    return await conn.fetchval(
        """INSERT INTO consent (session_id, aspsp_name, aspsp_country, valid_until, label)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (session_id) DO UPDATE
              SET valid_until = EXCLUDED.valid_until, status = 'active', last_error = NULL,
                  label = coalesce(EXCLUDED.label, consent.label)
           RETURNING id""", session_id, aspsp_name, country, valid_until, label)


async def set_consent_label(conn, consent_id: int, label: str | None) -> None:
    await conn.execute("UPDATE consent SET label = $2 WHERE id = $1", consent_id, label)


async def consents(conn, active_only: bool = True) -> list:
    return await conn.fetch(
        "SELECT * FROM consent" + (" WHERE status = 'active'" if active_only else "")
        + " ORDER BY id")


async def consent_by_session(conn, session_id: str):
    return await conn.fetchrow("SELECT * FROM consent WHERE session_id = $1", session_id)


async def mark_consent(conn, consent_id: int, *, status: str | None = None,
                       error: str | None = None, synced: bool = False,
                       warned: bool = False) -> None:
    await conn.execute(
        """UPDATE consent SET status = coalesce($2, status),
                              last_error = $3,
                              last_sync_at = CASE WHEN $4 THEN now() ELSE last_sync_at END,
                              warned_at = CASE WHEN $5 THEN now() ELSE warned_at END
           WHERE id = $1""", consent_id, status, error, synced, warned)


async def supersede_consents(conn, aspsp_name: str, keep_id: int) -> list:
    """A new consent replaces older ones for the same bank that it fully
    covers: a renewal by the same person. Another person's consent, which
    reaches accounts this one does not, stays. Returns the ids revoked."""
    rows = await conn.fetch(
        """UPDATE consent c SET status = 'revoked', last_error = 'replaced by a newer consent'
           WHERE c.aspsp_name = $1 AND c.id <> $2 AND c.status = 'active'
             AND NOT EXISTS (
                 SELECT 1 FROM consent_account old
                 WHERE old.consent_id = c.id
                   AND NOT EXISTS (SELECT 1 FROM consent_account new
                                   WHERE new.consent_id = $2
                                     AND new.account_id = old.account_id))
           RETURNING c.id""", aspsp_name, keep_id)
    return [r["id"] for r in rows]


# -- accounts

async def save_account(conn, ident: dict, *, aspsp_name: str, consent_id: int,
                       is_business_income: bool) -> int:
    return await conn.fetchval(
        """INSERT INTO account (account_key, source, aspsp_name, current_uid, consent_id,
                                name, product, cash_account_type, currency, iban, bban,
                                is_business_income)
           VALUES ($1, 'enable_banking', $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
           ON CONFLICT (account_key) DO UPDATE
              SET current_uid = EXCLUDED.current_uid,
                  consent_id = EXCLUDED.consent_id,
                  aspsp_name = EXCLUDED.aspsp_name,
                  name = coalesce(EXCLUDED.name, account.name),
                  -- DNB puts each user's own nickname first ("Felles, BRUKSKONTO").
                  -- Another person's session shows the plain product: keep the nickname.
                  product = CASE WHEN account.product LIKE '%,%'
                                      AND coalesce(EXCLUDED.product, '') NOT LIKE '%,%'
                                 THEN account.product
                                 ELSE coalesce(EXCLUDED.product, account.product) END,
                  cash_account_type = coalesce(EXCLUDED.cash_account_type, account.cash_account_type),
                  iban = coalesce(EXCLUDED.iban, account.iban),
                  bban = coalesce(EXCLUDED.bban, account.bban),
                  is_business_income = EXCLUDED.is_business_income,
                  active = true,
                  updated_at = now()
           RETURNING id""",
        ident["account_key"], aspsp_name, ident["uid"], consent_id, ident["name"],
        ident["product"], ident["cash_account_type"], ident["currency"], ident["iban"],
        ident["bban"], is_business_income)


async def link_account(conn, consent_id: int, account_id: int, uid: str) -> None:
    await conn.execute(
        """INSERT INTO consent_account (consent_id, account_id, uid) VALUES ($1, $2, $3)
           ON CONFLICT (consent_id, account_id) DO UPDATE SET uid = EXCLUDED.uid""",
        consent_id, account_id, uid)


async def linked_accounts(conn, consent_id: int) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM consent_account WHERE consent_id = $1", consent_id)


# An account under several active consents is synced once, under the one
# that lasts longest (ties: the newest), so the nightly request budget per
# account is not spent twice.
CONSENT_ACCOUNTS = """
SELECT a.*, ca.uid
FROM consent_account ca
JOIN account a ON a.id = ca.account_id
JOIN consent me ON me.id = ca.consent_id
WHERE ca.consent_id = $1 AND a.active
  AND NOT EXISTS (
      SELECT 1 FROM consent_account o
      JOIN consent other ON other.id = o.consent_id
      WHERE o.account_id = ca.account_id AND other.id <> me.id AND other.status = 'active'
        AND (other.valid_until > me.valid_until
             OR (other.valid_until = me.valid_until AND other.id > me.id)))
ORDER BY a.id
"""


async def consent_accounts(conn, consent_id: int) -> list:
    """The accounts this consent is responsible for syncing, each with the
    uid this consent's session gave it."""
    return await conn.fetch(CONSENT_ACCOUNTS, consent_id)


async def newest_booking(conn, account_id: int) -> date | None:
    return await conn.fetchval(
        "SELECT max(booking_date) FROM txn WHERE account_id = $1", account_id)


# -- imports

async def start_batch(conn, *, source: str, kind: str, account_id: int | None = None,
                      consent_id: int | None = None, date_from=None) -> int:
    return await conn.fetchval(
        """INSERT INTO import_batch (source, kind, account_id, consent_id, date_from)
           VALUES ($1, $2, $3, $4, $5) RETURNING id""",
        source, kind, account_id, consent_id, _date(date_from))


async def finish_batch(conn, batch_id: int, *, pages: int = 0, seen: int = 0, added: int = 0,
                       error: str | None = None) -> None:
    await conn.execute(
        """UPDATE import_batch SET finished_at = now(), pages = $2, seen = $3, added = $4,
                                   error = $5 WHERE id = $1""",
        batch_id, pages, seen, added, error)


def sha256_json(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False)
                          .encode()).hexdigest()


async def store_raw(conn, batch_id: int, kind: str, payload=None, *, content: bytes | None = None,
                    filename: str | None = None) -> int:
    digest = hashlib.sha256(content).hexdigest() if content is not None else sha256_json(payload)
    return await conn.fetchval(
        """INSERT INTO raw_record (batch_id, kind, sha256, payload, content, filename)
           VALUES ($1, $2, $3, $4::jsonb, $5, $6) RETURNING id""",
        batch_id, kind, digest, json.dumps(payload) if payload is not None else None,
        content, filename)


# A row is new unless the bank's reference or the fingerprint is already
# there. Checking the fingerprint for referenced rows too protects against
# banks whose references change between downloads.
INSERT_TXN = """
INSERT INTO txn (account_id, booking_date, value_date, amount_minor, currency,
                 original_amount, original_currency, exchange_rate, description,
                 counterparty, external_ref, fingerprint, batch_id, raw_record_id,
                 card_holder, extra)
SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16::jsonb
WHERE NOT EXISTS (SELECT 1 FROM txn WHERE account_id = $1 AND fingerprint = $12)
ON CONFLICT DO NOTHING
RETURNING id
"""


async def insert_transactions(conn, account_id: int, batch_id: int, rows: list[dict]) -> int:
    """Insert normalised rows (with fingerprint and raw_record_id). Returns
    how many were new."""
    added = 0
    async with conn.transaction():
        for r in rows:
            new_id = await conn.fetchval(
                INSERT_TXN, account_id, _date(r["booking_date"]), _date(r.get("value_date")),
                r["amount_minor"], r["currency"], r.get("original_amount"),
                r.get("original_currency"), r.get("exchange_rate"), r["description"],
                r.get("counterparty"), r.get("external_ref"), r["fingerprint"], batch_id,
                r.get("raw_record_id"), r.get("card_holder"),
                json.dumps(r["extra"], default=str) if r.get("extra") else None)
            if new_id is not None:
                added += 1
    return added


async def save_balances(conn, account_id: int, batch_id: int, rows: list[dict]) -> None:
    for r in rows:
        await conn.execute(
            """INSERT INTO balance (account_id, as_of, balance_type, amount_minor, currency, batch_id)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (account_id, as_of, balance_type) DO UPDATE
                  SET amount_minor = EXCLUDED.amount_minor, currency = EXCLUDED.currency,
                      batch_id = EXCLUDED.batch_id""",
            account_id, _date(r["as_of"]), r["balance_type"], r["amount_minor"],
            r["currency"], batch_id)


# -- settings

async def get_setting(conn, key: str, default: str | None = None) -> str | None:
    value = await conn.fetchval("SELECT value FROM app_setting WHERE key = $1", key)
    return default if value is None else value


async def set_setting(conn, key: str, value: str) -> None:
    await conn.execute(
        """INSERT INTO app_setting (key, value) VALUES ($1, $2)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", key, value)
