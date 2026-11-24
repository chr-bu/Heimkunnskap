"""Files by email: the worker reads a dedicated mailbox over IMAP every few
minutes and imports the PDF and Excel attachments of trusted mail, the same
way as an upload (fileimport.py): card exports, pay slips, expense claims.

Trust. Anyone can send mail to the address, and a From line is easy to
forge, so a message is used only if both hold:

  - its From address is in MAIL_ALLOWED_SENDERS, and
  - Gmail's own sender checks pass for that address's domain: DMARC, or
    a DKIM signature by that domain, or SPF for it. They are read from the
    topmost Authentication-Results header with Gmail's authserv-id
    (mx.google.com), which Gmail adds on receipt; any such header further
    down came with the message and is ignored.

Only attachments named .pdf or .xlsx whose contents start as a PDF or an
xlsx (zip) file, and no larger than MAIL_MAX_MB, are imported. Nothing in
the message is followed or opened otherwise.

Every message read is marked read and copied to the label Heimkunnskap/
Imported or Heimkunnskap/Rejected, so the mailbox shows what happened. A
Pushover message reports each import, and mail from a trusted address that
fails the checks. Mail from anyone else is labelled Rejected quietly.

The app password lives in a file (MAIL_PASSWORD_FILE), mounted read-only
into the worker, never in .env or the repository.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import imaplib
import logging
import re
from dataclasses import dataclass, field
from email.utils import getaddresses

from . import db, fileimport, notify
from .config import settings

log = logging.getLogger("heimkunnskap.mailin")

AUTHSERV = "mx.google.com"
LABEL_OK, LABEL_BAD = "Heimkunnskap/Imported", "Heimkunnskap/Rejected"
KINDS = {".pdf": b"%PDF-", ".xlsx": b"PK\x03\x04"}


@dataclass
class Verdict:
    ok: bool
    sender: str
    reason: str = ""


def _domain_of(value: str) -> str:
    value = value.strip().strip("<>").lower()
    return value.rsplit("@", 1)[-1]


def _aligned(domain: str, from_domain: str) -> bool:
    """The authenticated domain is the From domain or a subdomain of it."""
    return domain == from_domain or domain.endswith("." + from_domain)


def sender_check(msg, allowed: tuple[str, ...], authserv: str = AUTHSERV) -> Verdict:
    """Whether a message is from a trusted sender, by its From address and
    the receiving server's authentication results."""
    froms = msg.get_all("From") or []
    addresses = [a for _, a in getaddresses([str(f) for f in froms]) if a]
    if len(addresses) != 1:
        return Verdict(False, "", "not exactly one From address")
    sender = addresses[0].lower()
    if sender not in {a.lower() for a in allowed}:
        return Verdict(False, sender, "sender not on the list")
    from_domain = _domain_of(sender)
    results = None
    for header in msg.get_all("Authentication-Results") or []:
        header = re.sub(r"\s+", " ", str(header))
        if header.split(";", 1)[0].strip().lower() == authserv:
            results = header
            break
    if results is None:
        return Verdict(False, sender, f"no Authentication-Results from {authserv}")
    passed = set()
    for clause in results.split(";")[1:]:
        clause = clause.strip()
        m = re.match(r"(dkim|spf|dmarc)=(\w+)", clause, re.I)
        if not m or m.group(2).lower() != "pass":
            continue
        method = m.group(1).lower()
        props = dict((k.lower(), v) for k, v in re.findall(r"([\w.]+)=(\S+)", clause[m.end():]))
        if method == "dmarc" and _domain_of(props.get("header.from", "")) == from_domain:
            passed.add(method)
        if method == "dkim" and any(_aligned(_domain_of(props[k]), from_domain)
                                    for k in ("header.i", "header.d") if k in props):
            passed.add(method)
        if method == "spf" and _aligned(_domain_of(props.get("smtp.mailfrom", "")), from_domain):
            passed.add(method)
    for method in ("dmarc", "dkim", "spf"):           # the strongest that passed
        if method in passed:
            return Verdict(True, sender, f"{method}=pass")
    return Verdict(False, sender, "Gmail's sender checks did not pass for "
                                  f"{from_domain} (a forged or forwarded message?)")


@dataclass
class Attachment:
    filename: str
    content: bytes


def attachments(msg, max_bytes: int) -> tuple[list[Attachment], list[str]]:
    """The PDF and xlsx files in a message, and why any others were skipped."""
    found, skipped = [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename()
        if not name:
            continue
        name = re.sub(r"[\x00-\x1f/\\]", "_", name).strip()[:200] or "attachment"
        ext = next((e for e in KINDS if name.lower().endswith(e)), None)
        if ext is None:
            skipped.append(f"{name}: not a PDF or Excel file")
            continue
        content = part.get_payload(decode=True) or b""
        if len(content) > max_bytes:
            skipped.append(f"{name}: larger than {max_bytes // 1_000_000} MB")
        elif not content.startswith(KINDS[ext]):
            skipped.append(f"{name}: its contents are not a {ext[1:].upper()} file")
        else:
            found.append(Attachment(name, content))
    return found, skipped


@dataclass
class Handled:
    verdict: Verdict
    results: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def imported(self) -> bool:
        return self.verdict.ok and any(r["status"] in ("imported", "duplicate") for r in self.results)


async def handle(conn, raw: bytes) -> Handled:
    """One message: check the sender, import what it carries."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    verdict = sender_check(msg, settings.mail_allowed_senders)
    if not verdict.ok:
        return Handled(verdict)
    files, skipped = attachments(msg, settings.mail_max_mb * 1_000_000)
    results = []
    for f in files:
        try:
            results.append(await fileimport.import_file(conn, f.filename, f.content, via="mail"))
        except Exception as exc:                         # one bad file must not stop the rest
            log.exception("mail attachment %s failed", f.filename)
            results.append({"filename": f.filename, "status": "rejected",
                            "reason": f"import failed: {type(exc).__name__}"})
    return Handled(verdict, results, skipped)


def report(h: Handled) -> tuple[str, str, int] | None:
    """The alert (title, body, priority) for a handled mail, or None to
    stay quiet. Imports are confirmations, sent without sound."""
    v = h.verdict
    if not v.ok:
        if v.reason == "sender not on the list" or not v.sender:
            return None
        return ("mail refused", f"From {v.sender}: {v.reason}. Nothing was imported.", 0)
    lines = []
    for r in h.results:
        if r["status"] == "imported":
            lines.append(f"{r['filename']}: {r['account']}, {r['period']}"
                         + (f" ({r['added']} new)" if r.get("kind") not in ("payslip", "expense_claim")
                            else ""))
            lines += [f"  {c}" for c in r.get("checks", [])[:4]]
        elif r["status"] == "duplicate":
            lines.append(f"{r['filename']}: already imported")
        else:
            lines.append(f"{r['filename']}: not imported: {r['reason']}")
    lines += [f"{s} (skipped)" for s in h.skipped]
    if not lines:
        lines = ["no PDF or Excel attachment in the message"]
    problems = any(r["status"] == "rejected" for r in h.results) or not h.results
    return (f"mail from {v.sender}", "\n".join(lines), 0 if problems else notify.QUIET)


class Mailbox:
    """A thin, blocking IMAP client; the worker calls it in a thread."""

    def __init__(self, host: str, user: str, password: str):
        self.imap = imaplib.IMAP4_SSL(host, timeout=60)
        self.imap.login(user, password)
        for label in (LABEL_OK, LABEL_BAD):
            self.imap.create(f'"{label}"')          # an existing label answers NO: fine
        self.imap.select("INBOX")

    def unseen(self) -> list[bytes]:
        typ, data = self.imap.uid("SEARCH", None, "UNSEEN")
        return data[0].split() if typ == "OK" and data and data[0] else []

    def fetch(self, uid: bytes) -> bytes | None:
        typ, data = self.imap.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK":
            return None
        return next((d[1] for d in data if isinstance(d, tuple)), None)

    def done(self, uid: bytes, label: str) -> None:
        self.imap.uid("COPY", uid, f'"{label}"')
        self.imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")

    def close(self) -> None:
        try:
            self.imap.close()
            self.imap.logout()
        except Exception:
            pass


def _password() -> str:
    with open(settings.mail_password_file) as f:
        return f.read().strip().replace(" ", "")      # Google shows it in groups of four


async def poll_once(mailbox_factory=None) -> int:
    """Read every unread message once. Returns how many were handled."""
    factory = mailbox_factory or (lambda: Mailbox(settings.mail_imap_host, settings.mail_user,
                                                  _password()))
    box = await asyncio.to_thread(factory)
    conn = await db.connect()
    count = 0
    try:
        for uid in await asyncio.to_thread(box.unseen):
            raw = await asyncio.to_thread(box.fetch, uid)
            if raw is None:
                continue
            h = await handle(conn, raw)
            await asyncio.to_thread(box.done, uid, LABEL_OK if h.imported else LABEL_BAD)
            log.info("mail %s from %s: %s, %d files", uid.decode(), h.verdict.sender or "?",
                     h.verdict.reason, len(h.results))
            message = report(h)
            if message:
                await asyncio.to_thread(notify.send, *message)
            count += 1
    finally:
        await conn.close()
        await asyncio.to_thread(box.close)
    return count


async def loop() -> None:
    """Poll for good, every MAIL_POLL_SECONDS. A failure is logged, and
    reported once until polling works again."""
    log.info("mail intake: %s every %d s, from %s", settings.mail_user, settings.mail_poll_seconds,
             ", ".join(settings.mail_allowed_senders))
    failing = False
    while True:
        try:
            await poll_once()
            if failing:
                notify.send("mail intake works again")
            failing = False
        except Exception as exc:
            log.exception("mail intake failed")
            if not failing:
                notify.send("mail intake failed", f"{type(exc).__name__}: {str(exc)[:300]}")
            failing = True
        await asyncio.sleep(settings.mail_poll_seconds)


def enabled() -> bool:
    return bool(settings.mail_user and settings.mail_allowed_senders)
