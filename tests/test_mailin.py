"""Mail intake: who is trusted, what is imported. Synthetic addresses only."""

import dataclasses
import email
import email.policy
from email.message import EmailMessage

import pytest

from heimkunnskap import mailin

ALLOWED = ("ola@example.org", "kari@example.net")
GOOD = ("mx.google.com; dkim=pass header.i=@example.org header.s=s1 header.b=abc; "
        "spf=pass (google.com: domain of ola@example.org designates 192.0.2.1 as permitted "
        "sender) smtp.mailfrom=ola@example.org; dmarc=pass (p=REJECT sp=REJECT dis=NONE) "
        "header.from=example.org")


def message(sender="Ola <ola@example.org>", results=(GOOD,), files=(), extra_headers=()):
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "intake@example.com"
    m["Subject"] = "Reiseregning"
    for name, value in extra_headers:
        m[name] = value
    m.set_content("Vedlagt.")
    for name, content, ctype in files:
        maintype, subtype = ctype.split("/")
        m.add_attachment(content, maintype=maintype, subtype=subtype, filename=name)
    raw = m.as_bytes()
    # The receiving server adds its results on top, as Gmail does
    top = b"".join(f"Authentication-Results: {r}\r\n".encode() for r in results)
    return top + raw


def parsed(raw):
    return email.message_from_bytes(raw, policy=email.policy.default)


def check(raw, allowed=ALLOWED):
    return mailin.sender_check(parsed(raw), allowed)


def test_a_trusted_sender_with_passing_checks():
    v = check(message())
    assert v.ok and v.sender == "ola@example.org" and v.reason == "dmarc=pass"
    dkim_only = "mx.google.com; dkim=pass header.i=@mail.example.org; spf=fail; dmarc=none"
    assert check(message(results=(dkim_only,))).reason == "dkim=pass"     # a subdomain aligns
    spf_only = "mx.google.com; dkim=none; spf=pass smtp.mailfrom=bounce@example.org"
    assert check(message(results=(spf_only,))).reason == "spf=pass"


def test_forged_and_unknown_senders_are_refused():
    v = check(message(sender="Someone <someone@example.com>"))
    assert not v.ok and v.reason == "sender not on the list"
    # A forged From: Gmail's own results fail; a header the sender wrote lower down
    # claiming to be Gmail's is ignored
    failing = ("mx.google.com; dkim=pass header.i=@attacker.example; spf=pass "
               "smtp.mailfrom=x@attacker.example; dmarc=fail header.from=example.org")
    v = check(message(results=(failing,),
                      extra_headers=[("Authentication-Results", GOOD)]))
    assert not v.ok and "did not pass for example.org" in v.reason
    v = check(message(results=()))
    assert not v.ok and "no Authentication-Results from mx.google.com" in v.reason
    v = check(message(results=("evil.example; dmarc=pass header.from=example.org",)))
    assert not v.ok
    v = check(message(sender="ola@example.org, kari@example.net"))
    assert not v.ok and v.reason == "not exactly one From address"
    unaligned = "mx.google.com; dkim=pass header.i=@notexample.org"
    assert not check(message(results=(unaligned,))).ok


def test_only_real_pdf_and_excel_files_are_taken():
    raw = message(files=[("expense_1.pdf", b"%PDF-1.7 claim", "application/pdf"),
                         ("transactions.xlsx", b"PK\x03\x04 sheet", "application/octet-stream"),
                         ("invoice.pdf", b"MZ not a pdf", "application/pdf"),
                         ("run.exe", b"MZ", "application/octet-stream"),
                         ("../../etc/big.pdf", b"%PDF-" + b"x" * 2_000_001, "application/pdf")])
    found, skipped = mailin.attachments(parsed(raw), max_bytes=2_000_000)
    assert [(f.filename, f.content[:5]) for f in found] == [("expense_1.pdf", b"%PDF-"),
                                                             ("transactions.xlsx", b"PK\x03\x04 ")]
    assert any("invoice.pdf: its contents are not a PDF" in s for s in skipped)
    assert any("run.exe: not a PDF or Excel" in s for s in skipped)
    assert any(s.startswith(".._.._etc_big.pdf: larger than 2 MB") for s in skipped)


def test_what_is_reported():
    quiet = mailin.Handled(mailin.Verdict(False, "someone@example.com", "sender not on the list"))
    assert mailin.report(quiet) is None
    loud = mailin.Handled(mailin.Verdict(False, "ola@example.org", "Gmail's sender checks did not pass"))
    assert mailin.report(loud)[0] == "mail refused"
    empty = mailin.Handled(mailin.Verdict(True, "ola@example.org", "dmarc=pass"))
    assert "no PDF or Excel attachment" in mailin.report(empty)[1]
    assert not empty.imported


class FakeBox:
    def __init__(self, messages):
        self.messages = dict(messages)
        self.labels = {}

    def unseen(self):
        return [uid for uid in self.messages if uid not in self.labels]

    def fetch(self, uid):
        return self.messages[uid]

    def done(self, uid, label):
        self.labels[uid] = label

    def close(self):
        pass


@pytest.fixture
def mail_settings(monkeypatch):
    monkeypatch.setattr(mailin, "settings", dataclasses.replace(
        mailin.settings, mail_user="intake@example.com", mail_allowed_senders=ALLOWED,
        mail_max_mb=5))


def test_polling_imports_trusted_mail_and_labels_everything(mail_settings, monkeypatch):
    imported, sent = [], []

    async def fake_import(conn, filename, content, via="upload", **kw):
        imported.append((filename, via))
        return {"filename": filename, "status": "imported", "kind": "expense_claim",
                "account": "Expense claim 100001, Eksempel", "period": "04.05-06.05.2026",
                "rows": 7, "added": 7, "checks": ["paid out 15 800.00 on 19.06.2026"]}

    class Conn:
        async def close(self):
            pass

    async def connect():
        return Conn()

    monkeypatch.setattr(mailin.fileimport, "import_file", fake_import)
    monkeypatch.setattr(mailin.db, "connect", connect)
    monkeypatch.setattr(mailin.notify, "send", lambda title, body="", priority=0: sent.append((title, body)))
    box = FakeBox({b"1": message(files=[("expense_100001.pdf", b"%PDF-1.7", "application/pdf")]),
                   b"2": message(sender="spam@example.com",
                                 files=[("x.pdf", b"%PDF-1.7", "application/pdf")])})
    import asyncio
    assert asyncio.run(mailin.poll_once(lambda: box)) == 2
    assert imported == [("expense_100001.pdf", "mail")]
    assert box.labels == {b"1": mailin.LABEL_OK, b"2": mailin.LABEL_BAD}
    assert len(sent) == 1 and sent[0][0] == "mail from ola@example.org"
    good = mailin.Handled(mailin.Verdict(True, "ola@example.org", "dmarc=pass"),
                          [{"filename": "a.pdf", "status": "duplicate"}])
    assert mailin.report(good)[2] == mailin.notify.QUIET          # a confirmation: no sound
    nothing = mailin.Handled(mailin.Verdict(True, "ola@example.org", "dmarc=pass"))
    assert mailin.report(nothing)[2] == 0                          # nothing imported: a sound
    assert "paid out 15 800.00" in sent[0][1]
    assert asyncio.run(mailin.poll_once(lambda: box)) == 0         # read once only
