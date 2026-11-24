"""The EuroBonus account's history, from the Excel export on sas.no (My
EuroBonus, Transactions, export): one sheet with the columns Date,
Activity, Bonus Points and Level Points, newest first, the whole history.

Activity is "Partner | Kind" ("Amex Norway Reward | Extra Points"),
sometimes with a middle part (an online shop), and for flights the route
("Oslo - London | Basic Points"). Points spent are negative.

Each export repeats the whole history, so rows are matched on a
fingerprint (day, activity, points, and the row's place among equal rows
of that day) and only new ones are added. See docs/formats/eurobonus.md.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime

HEADER = ("Date", "Activity", "Bonus Points", "Level Points")


class EuroBonusError(ValueError):
    pass


@dataclass
class Row:
    day: date
    activity: str
    partner: str
    kind: str
    bonus_points: int
    level_points: int
    fingerprint: str = ""


def _sheet_rows(content: bytes):
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise EuroBonusError(f"not a readable Excel file: {type(exc).__name__}") from None
    return list(wb.worksheets[0].iter_rows(values_only=True))


def detect(content: bytes) -> bool:
    if not content.startswith(b"PK\x03\x04"):
        return False
    try:
        rows = _sheet_rows(content)
    except EuroBonusError:
        return False
    return bool(rows) and tuple(str(c or "").strip() for c in rows[0][:4]) == HEADER


def _day(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def _points(value) -> int:
    if value in (None, ""):
        return 0
    return int(round(float(str(value).replace(" ", "").replace(",", "."))))


def split_activity(activity: str) -> tuple[str, str]:
    """(partner, kind). "Oslo - Bangkok | " (points spent) has no kind."""
    parts = [p.strip() for p in re.split(r"\s*\|\s*", activity) if p.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def parse(content: bytes) -> list[Row]:
    rows = _sheet_rows(content)
    if not rows or tuple(str(c or "").strip() for c in rows[0][:4]) != HEADER:
        raise EuroBonusError("not a EuroBonus export (columns Date, Activity, Bonus Points, Level Points)")
    out: list[Row] = []
    seen: dict[tuple, int] = {}
    for n, r in enumerate(rows[1:], 2):
        if not r or all(c in (None, "") for c in r[:4]):
            continue
        try:
            day = _day(r[0])
            activity = str(r[1] or "").strip()
            bonus, level = _points(r[2]), _points(r[3])
        except (ValueError, TypeError):
            raise EuroBonusError(f"row {n}: date or points not understood") from None
        partner, kind = split_activity(activity)
        key = (day, activity, bonus, level)
        seen[key] = seen.get(key, 0) + 1
        fp = hashlib.sha256(f"{day}|{activity}|{bonus}|{level}|{seen[key]}".encode()).hexdigest()[:32]
        out.append(Row(day, activity, partner, kind, bonus, level, fp))
    if not out:
        raise EuroBonusError("the export has no rows")
    return out
