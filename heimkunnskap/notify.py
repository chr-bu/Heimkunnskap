"""Alerts as Pushover messages to the phone.

The credentials live in a file (PUSHOVER_FILE), mounted read-only: two
lines, the application's API token and the user (or group) key, either
labelled or bare, token first:

    token=<application API token>        <application API token>
    user=<user key>                      <user key>

Without the file, alerts only go to the log. An alert never breaks a sync.

Priority: 0 (the default) for anything that needs doing, such as a consent
to renew or a failed sync; -1, delivered without sound, for confirmations
such as a file imported by mail. Nothing uses 1 or 2, which break through
quiet hours: the nightly sync runs at 02:30. A link in the body becomes the
message's link, so the button opens the page to act on.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
import urllib.request

from .config import settings

log = logging.getLogger("heimkunnskap.notify")

API = "https://api.pushover.net/1/messages.json"
URL = re.compile(r"https?://\S+")
QUIET = -1


def credentials(path: str) -> tuple[str, str] | None:
    """(token, user) from the credentials file, or None."""
    try:
        with open(path) as f:
            lines = [line.strip() for line in f if line.strip()]
    except OSError:
        return None
    if all("=" in line for line in lines):
        values = dict(line.split("=", 1) for line in lines)
    elif len(lines) == 2 and not any("=" in line for line in lines):
        values = {"token": lines[0], "user": lines[1]}           # bare: token, then user
    else:
        return None
    token, user = values.get("token", "").strip(), values.get("user", "").strip()
    return (token, user) if token and user else None


def message(title: str, body: str = "", priority: int = 0) -> dict:
    """The Pushover fields for an alert, within Pushover's limits."""
    fields = {"title": f"Heimkunnskap: {title}"[:250],
              "message": (body or title)[:1024],
              "priority": str(priority)}
    link = URL.search(body or "")
    if link:
        fields["url"] = link.group().rstrip(".,)")[:512]
    return fields


def send(title: str, body: str = "", priority: int = 0) -> bool:
    log.warning("alert: %s %s", title, body)
    creds = credentials(settings.pushover_file) if settings.pushover_file else None
    if creds is None:
        return False
    token, user = creds
    data = urllib.parse.urlencode({"token": token, "user": user,
                                   **message(title, body, priority)}).encode()
    req = urllib.request.Request(API, data=data, method="POST",
                                 headers={"User-Agent": "heimkunnskap"})
    try:
        with urllib.request.urlopen(req, timeout=20):
            return True
    except Exception as exc:                      # an alert must never break a sync
        log.error("could not send alert to Pushover: %s", type(exc).__name__)
        return False
