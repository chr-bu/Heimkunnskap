from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from heimkunnskap.sync import card_file_due, date_from_for, needs_expiry_warning, next_run, seconds_until

TODAY = date(2026, 11, 15)


def test_date_from():
    assert date_from_for(full=True, newest=None, today=TODAY) == TODAY - timedelta(days=1825)
    assert date_from_for(full=False, newest=None, today=TODAY) == TODAY - timedelta(days=89)
    assert date_from_for(full=False, newest=date(2026, 11, 14), today=TODAY) == date(2026, 11, 4)
    # a long outage cannot reach past the unattended window
    assert date_from_for(full=False, newest=date(2026, 1, 1), today=TODAY) == \
        TODAY - timedelta(days=89)


def test_next_run_local_time_and_dst():
    oslo = ZoneInfo("Europe/Oslo")
    now = datetime(2026, 11, 15, 1, 0, tzinfo=oslo)
    assert next_run(now, "02:30") == datetime(2026, 11, 15, 2, 30, tzinfo=oslo)
    now = datetime(2026, 11, 15, 2, 30, tzinfo=oslo)
    assert next_run(now, "02:30") == datetime(2026, 11, 16, 2, 30, tzinfo=oslo)
    # across the spring clock change: wall-clock time, real seconds
    now = datetime(2027, 3, 27, 12, 0, tzinfo=oslo)
    assert next_run(now, "04:00") == datetime(2027, 3, 28, 4, 0, tzinfo=oslo)
    assert seconds_until(datetime(2027, 3, 28, 4, 0, tzinfo=oslo),
                         datetime(2027, 3, 28, 1, 0, tzinfo=oslo)) == 2 * 3600


def test_expiry_warning_window_and_repeat():
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    assert not needs_expiry_warning(now + timedelta(days=30), None, now)
    assert needs_expiry_warning(now + timedelta(days=10), None, now)
    assert not needs_expiry_warning(now + timedelta(days=10), now - timedelta(days=1), now)
    assert needs_expiry_warning(now + timedelta(days=10), now - timedelta(days=3), now)


def test_card_file_due():
    today = date(2026, 12, 20)
    assert not card_file_due(None, None, today)                       # no file yet
    assert not card_file_due(date(2026, 11, 15), None, today)         # 35 days
    assert card_file_due(date(2026, 11, 5), None, today)              # 45 days
    assert not card_file_due(date(2026, 11, 5), date(2026, 12, 15), today)
    assert card_file_due(date(2026, 11, 5), date(2026, 12, 13), today)


def test_last_run():
    from heimkunnskap.sync import last_run
    oslo = ZoneInfo("Europe/Oslo")
    assert last_run(datetime(2026, 11, 25, 2, 31, tzinfo=oslo), "02:30") == \
        datetime(2026, 11, 25, 2, 30, tzinfo=oslo)
    assert last_run(datetime(2026, 11, 25, 2, 29, tzinfo=oslo), "02:30") == \
        datetime(2026, 11, 24, 2, 30, tzinfo=oslo)


class InterfaceError(Exception):
    """Named like asyncpg's: the connection is closed."""


class ConnectionDoesNotExistError(InterfaceError):
    pass


def test_connection_lost_by_class_name():
    from heimkunnskap.sync import connection_lost
    assert connection_lost(InterfaceError("connection is closed"))
    assert connection_lost(ConnectionDoesNotExistError())             # a subclass
    assert connection_lost(ConnectionResetError())
    assert not connection_lost(ValueError("bad data"))


def test_nightly_tries_again_after_losing_the_database(monkeypatch):
    import asyncio
    from heimkunnskap import notify, sync
    alerts = []
    monkeypatch.setattr(notify, "send", lambda title, body="", priority=0: alerts.append((title, body)))
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise InterfaceError("connection is closed")

    assert asyncio.run(sync.nightly(flaky, attempts=3, wait=0)) is True
    assert len(calls) == 3 and alerts == []                           # recovered: no alert

    async def gone():
        raise InterfaceError("connection is closed")

    assert asyncio.run(sync.nightly(gone, attempts=2, wait=0)) is False
    assert alerts == [("nightly sync failed", "InterfaceError after 2 attempts")]

    calls.clear()

    async def broken():
        calls.append(1)
        raise ValueError("bad data")                                  # not retried

    alerts.clear()
    assert asyncio.run(sync.nightly(broken, attempts=3, wait=0)) is False
    assert len(calls) == 1 and alerts == [("nightly sync failed", "ValueError")]
