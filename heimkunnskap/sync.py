"""Fetch from Enable Banking into the ledger.

The worker runs this once a night at SYNC_AT. The web app runs a full
import right after BankID, while history older than 90 days is still
readable (about an hour).

    python -m heimkunnskap.sync                # worker: migrate, then nightly loop
    python -m heimkunnskap.sync --once         # one nightly sync now
    python -m heimkunnskap.sync --once --full  # full history (only useful just after BankID)
    python -m heimkunnskap.sync --add-session ID [--full]
                                               # register a session made outside the app
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from . import banks, db, migrate, notify, pairing
from .config import settings
from .enablebanking import ApiError, EnableBanking, session_account_uids
from .normalise import (account_identity, add_fingerprints, balances_rows, masked,
                        matches_configured, normalise_transaction)

log = logging.getLogger("heimkunnskap.sync")

MAX_PAGES = 1000
LOCK_BASE = 0x4845494D  # "HEIM": advisory lock namespace, plus the consent id


# -- pure helpers

def date_from_for(*, full: bool, newest: date | None, today: date) -> date:
    if full:
        return today - timedelta(days=settings.full_history_days)
    if newest is None:
        return today - timedelta(days=settings.first_sync_days)
    # Never reach further back than the unattended 90-day window allows
    return max(newest - timedelta(days=settings.overlap_days),
               today - timedelta(days=settings.first_sync_days))


def next_run(now: datetime, at: str) -> datetime:
    """The next local time at HH:MM strictly after now (now is tz-aware)."""
    hh, mm = (int(x) for x in at.split(":"))
    candidate = datetime.combine(now.date(), dtime(hh, mm), tzinfo=now.tzinfo)
    if candidate <= now:
        candidate = datetime.combine(now.date() + timedelta(days=1), dtime(hh, mm),
                                     tzinfo=now.tzinfo)
    return candidate


def seconds_until(when: datetime, now: datetime) -> float:
    """Real seconds between two aware times. Subtracting datetimes that
    share a tzinfo ignores a clock change between them; UTC does not."""
    return (when.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()


def needs_expiry_warning(valid_until: datetime, warned_at: datetime | None,
                         now: datetime) -> bool:
    """Warn when inside the warning window, then again every three days."""
    if valid_until - now > timedelta(days=settings.consent_warn_days):
        return False
    return warned_at is None or now - warned_at >= timedelta(days=3)


def bank_short(aspsp_name: str) -> str | None:
    for short, (name, _country) in settings.banks.items():
        if name == aspsp_name:
            return short
    return None


def consent_link(aspsp_name: str, label: str | None = None) -> str:
    """The page that renews this consent: the same bank, the same person."""
    short = bank_short(aspsp_name)
    if short is None:
        return f"{settings.public_url}/"
    query = {"bank": short}
    if label:
        query["who"] = label
    return f"{settings.public_url}/eb/start?{urlencode(query)}"


def consent_title(consent) -> str:
    """'DNB' or 'DNB (Kari)', for alerts."""
    label = consent["label"]
    return f"{consent['aspsp_name']} ({label})" if label else consent["aspsp_name"]


def client() -> EnableBanking:
    return EnableBanking.from_files(settings.eb_app_id, settings.eb_key_file, settings.eb_api)


def _parse_ts(value) -> datetime:
    ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


# -- registering a consent

async def register_session(conn, eb: EnableBanking, session_id: str,
                           data: dict | None = None, label: str | None = None) -> int:
    """Store a BankID consent and its accounts. data is the POST /sessions
    response when there is one; otherwise the session is fetched. label says
    whose BankID it was ("Kari"). Older consents for the bank that this one
    fully covers are revoked; another person's consent stays."""
    if data is None:
        data = await asyncio.to_thread(eb.session, session_id)
    aspsp = data.get("aspsp") or {}
    valid_until = (data.get("access") or {}).get("valid_until")
    if not aspsp.get("name") or not valid_until:
        raise ValueError("session response lacks aspsp or access.valid_until")
    consent_id = await db.save_consent(conn, session_id, aspsp["name"],
                                       aspsp.get("country", "NO"), _parse_ts(valid_until), label)
    batch = await db.start_batch(conn, source="enable_banking", kind="session",
                                 consent_id=consent_id)
    await db.store_raw(conn, batch, "session", data)

    accounts = []
    for item in data.get("accounts") or []:
        if isinstance(item, dict) and (item.get("account_id") or item.get("identification_hash")):
            accounts.append(item)
    if not accounts:
        for uid in session_account_uids(data):
            details = await asyncio.to_thread(eb.account_details, uid)
            details.setdefault("uid", uid)
            await db.store_raw(conn, batch, "details", details)
            accounts.append(details)

    for details in accounts:
        ident = account_identity(details)
        business = matches_configured(settings.business_income_accounts,
                                      ident["iban"], ident["bban"])
        account_id = await db.save_account(conn, ident, aspsp_name=aspsp["name"],
                                           consent_id=consent_id, is_business_income=business)
        if ident["uid"]:
            await db.link_account(conn, consent_id, account_id, ident["uid"])
        log.info("account %s %s%s", masked(ident["bban"] or ident["iban"]),
                 ident["product"] or ident["name"] or "",
                 " (business income)" if business else "")
    await db.finish_batch(conn, batch, seen=len(accounts))
    revoked = await db.supersede_consents(conn, aspsp["name"], consent_id)
    log.info("consent %s for %s%s: %d accounts, valid until %s%s",
             consent_id, aspsp["name"], f" ({label})" if label else "", len(accounts), valid_until,
             f"; replaces consent {', '.join(map(str, revoked))}" if revoked else "")
    return consent_id


# -- syncing

async def sync_account(conn, eb: EnableBanking, account, consent_id: int, *, full: bool,
                       today: date) -> tuple[int, int]:
    newest = await db.newest_booking(conn, account["id"])
    since = date_from_for(full=full, newest=newest, today=today)
    batch = await db.start_batch(conn, source="enable_banking", kind="full" if full else "nightly",
                                 account_id=account["id"], consent_id=consent_id, date_from=since)
    pages = seen = added = 0
    profile = banks.for_bank(account["aspsp_name"])
    try:
        rows, key = [], None
        while True:
            data = await asyncio.to_thread(
                eb.transactions, account["uid"], since.isoformat(), key,
                "longest" if full else None)
            pages += 1
            raw_id = await db.store_raw(conn, batch, "transactions", data)
            for tx in data.get("transactions") or []:
                row = normalise_transaction(tx, iso646_names=profile.iso646_names)
                if row is not None:
                    row["raw_record_id"] = raw_id
                    rows.append(row)
            key = data.get("continuation_key")
            if not key or pages >= MAX_PAGES:
                break
        add_fingerprints(account["account_key"], rows)
        seen = len(rows)
        added = await db.insert_transactions(conn, account["id"], batch, rows)

        bal = await asyncio.to_thread(eb.balances, account["uid"])
        await db.store_raw(conn, batch, "balances", bal)
        await db.save_balances(conn, account["id"], batch, balances_rows(bal, today.isoformat()))
        with_ref = sum(1 for r in rows if r["external_ref"])
        log.info("account %s: %d pages, %d booked (%d with bank reference), %d new, from %s",
                 account["id"], pages, seen, with_ref, added, since)
        await db.finish_batch(conn, batch, pages=pages, seen=seen, added=added)
        return seen, added
    except Exception as exc:
        await db.finish_batch(conn, batch, pages=pages, seen=seen, added=added,
                              error=str(exc)[:500])
        raise


async def sync_consent(conn, eb: EnableBanking, consent, *, full: bool = False,
                       today: date | None = None) -> dict:
    """Sync every account under one consent. Returns a summary. An advisory
    lock keeps the worker and the web app from syncing one consent at once."""
    today = today or datetime.now(ZoneInfo(settings.timezone)).date()
    summary = {"consent": consent["id"], "bank": consent_title(consent), "accounts": 0,
               "seen": 0, "added": 0, "errors": []}
    if not await conn.fetchval("SELECT pg_try_advisory_lock($1, $2)", LOCK_BASE, consent["id"]):
        summary["errors"].append("another sync of this consent is running")
        return summary
    try:
        if not await db.linked_accounts(conn, consent["id"]):
            # A consent registered outside the web flow: learn its accounts
            await register_session(conn, eb, consent["session_id"], label=consent["label"])
        # May be empty: every account can be covered by a longer-lasting consent
        accounts = await db.consent_accounts(conn, consent["id"])
        for account in accounts:
            try:
                seen, added = await sync_account(conn, eb, account, consent["id"],
                                                 full=full, today=today)
                summary["accounts"] += 1
                summary["seen"] += seen
                summary["added"] += added
            except ApiError as exc:
                if exc.consent_gone:
                    await db.mark_consent(conn, consent["id"], status="expired", error=str(exc))
                    notify.send(f"{consent_title(consent)}: consent no longer valid",
                                "Renew with BankID: "
                                + consent_link(consent["aspsp_name"], consent["label"]))
                    summary["errors"].append("consent gone")
                    return summary
                summary["errors"].append(f"account {account['id']}: {exc}")
                if exc.rate_limited:
                    break                         # wait for the next night
        await db.mark_consent(conn, consent["id"], synced=True,
                              error="; ".join(summary["errors"])[:500] or None)
        return summary
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1, $2)", LOCK_BASE, consent["id"])


async def check_expiry(conn, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    for c in await db.consents(conn):
        if c["valid_until"] <= now:
            await db.mark_consent(conn, c["id"], status="expired", error="consent period ended")
            notify.send(f"{consent_title(c)}: consent expired",
                        "Nightly sync has stopped. Renew with BankID: "
                        + consent_link(c["aspsp_name"], c["label"]))
        elif needs_expiry_warning(c["valid_until"], c["warned_at"], now):
            days = max(0, (c["valid_until"] - now).days)
            notify.send(f"{consent_title(c)}: consent ends in {days} days",
                        f"Renew with BankID: {consent_link(c['aspsp_name'], c['label'])}")
            await db.mark_consent(conn, c["id"], warned=True, error=c["last_error"])


CARD_FILE_STALE_DAYS = 40      # a billing period is about a month
CARD_FILE_REMIND_DAYS = 7


def card_file_due(newest: date | None, warned: date | None, today: date) -> bool:
    """A card file is due when the newest card transaction is over 40 days
    old; remind once a week. A card with no file yet is not chased."""
    if newest is None or (today - newest).days <= CARD_FILE_STALE_DAYS:
        return False
    return warned is None or (today - warned).days >= CARD_FILE_REMIND_DAYS


async def check_card_files(conn, today: date | None = None) -> None:
    today = today or datetime.now(ZoneInfo(settings.timezone)).date()
    for card in await conn.fetch(
            """SELECT a.id, a.name, max(t.booking_date) AS newest
               FROM account a LEFT JOIN txn t ON t.account_id = a.id
               WHERE a.source = 'file' AND a.active GROUP BY a.id, a.name"""):
        key = f"card_file_warned:{card['id']}"
        warned = await db.get_setting(conn, key)
        if card_file_due(card["newest"], date.fromisoformat(str(warned)[:10]) if warned else None, today):
            notify.send(f"{card['name']}: time for a new card file",
                        f"The newest transaction is from {card['newest']:%d.%m.%Y}. Upload the "
                        f"latest export at {settings.public_url}/upload")
            await db.set_setting(conn, key, today.isoformat())


async def sync_all(*, full: bool = False) -> list[dict]:
    eb = client()
    conn = await db.connect()
    try:
        results = []
        for consent in await db.consents(conn):
            try:
                results.append(await sync_consent(conn, eb, consent, full=full))
            except Exception as exc:
                log.exception("consent %s failed", consent["id"])
                await db.mark_consent(conn, consent["id"], error=f"{type(exc).__name__}: {exc}"[:500])
                notify.send(f"{consent_title(consent)}: sync failed", type(exc).__name__)
        await check_expiry(conn)
        try:
            await pairing.run(conn)
        except Exception as exc:
            log.exception("pairing failed")
            notify.send("transfer pairing failed", type(exc).__name__)
        try:
            await check_card_files(conn)
        except Exception:
            log.exception("card file check failed")
        for r in results:
            log.info("synced %s: %d accounts, %d booked seen, %d new%s", r["bank"], r["accounts"],
                     r["seen"], r["added"], f", errors: {r['errors']}" if r["errors"] else "")
            if r["errors"] and r["errors"] != ["consent gone"]:
                notify.send(f"{r['bank']}: sync problems", "\n".join(r["errors"])[:1500])
        return results
    finally:
        await conn.close()


async def worker() -> None:
    await migrate.run()
    conn = await db.connect()
    try:
        await pairing.run(conn)        # classify at once after an upgrade
    except Exception:
        log.exception("pairing failed")
    finally:
        await conn.close()
    from . import business, mailin, trumf
    reminders = asyncio.create_task(business.loop())         # noqa: F841  (daytime, for good)
    trumf_task = asyncio.create_task(trumf.loop())           # noqa: F841  (daily, for good)
    if mailin.enabled():
        mail_task = asyncio.create_task(mailin.loop())     # noqa: F841  (runs for good)
    else:
        log.info("mail intake off: MAIL_USER or MAIL_ALLOWED_SENDERS not set")
    tz = ZoneInfo(settings.timezone)
    log.info("worker started; nightly sync at %s %s", settings.sync_at, settings.timezone)
    while True:
        wake = next_run(datetime.now(tz), settings.sync_at)
        await asyncio.sleep(max(1.0, seconds_until(wake, datetime.now(tz))))
        try:
            await sync_all()
        except Exception as exc:
            log.exception("nightly sync failed")
            notify.send("nightly sync failed", type(exc).__name__)


async def add_session(session_id: str, full: bool) -> None:
    eb = client()
    conn = await db.connect()
    try:
        consent_id = await register_session(conn, eb, session_id)
        consent = await conn.fetchrow("SELECT * FROM consent WHERE id = $1", consent_id)
        print(await sync_consent(conn, eb, consent, full=full))
    finally:
        await conn.close()


def main() -> None:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--once", action="store_true", help="sync now and exit")
    p.add_argument("--full", action="store_true", help="fetch full history")
    p.add_argument("--add-session", metavar="ID", help="register an existing session id")
    args = p.parse_args()
    if args.add_session:
        asyncio.run(add_session(args.add_session, args.full))
    elif args.once:
        asyncio.run(migrate.run())
        asyncio.run(sync_all(full=args.full))
    else:
        asyncio.run(worker())


if __name__ == "__main__":
    main()
