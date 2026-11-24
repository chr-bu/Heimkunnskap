import dataclasses
import urllib.parse

from heimkunnskap import notify


def test_credentials_and_the_message(tmp_path):
    f = tmp_path / "pushover"
    f.write_text("token=a1b2c3\nuser=u9u8u7\n")
    assert notify.credentials(str(f)) == ("a1b2c3", "u9u8u7")
    (tmp_path / "bare").write_text("a1b2c3\nu9u8u7\n\n")
    assert notify.credentials(str(tmp_path / "bare")) == ("a1b2c3", "u9u8u7")
    (tmp_path / "half").write_text("token=a1b2c3\n")
    assert notify.credentials(str(tmp_path / "half")) is None
    assert notify.credentials(str(tmp_path / "missing")) is None
    m = notify.message("DNB: consent ends in 9 days",
                       "Renew with BankID: https://heim.example.org/eb/start?bank=DNB.")
    assert m["title"] == "Heimkunnskap: DNB: consent ends in 9 days"
    assert m["url"] == "https://heim.example.org/eb/start?bank=DNB" and m["priority"] == "0"
    assert notify.message("x" * 300, "y" * 2000)["message"] == "y" * 1024
    assert notify.message("mail intake works again")["message"] == "mail intake works again"


def test_send(tmp_path, monkeypatch):
    f = tmp_path / "pushover"
    f.write_text("token=a1b2c3\nuser=u9u8u7\n")
    sent = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout):
        sent.append((req.full_url, dict(urllib.parse.parse_qsl(req.data.decode()))))
        return Response()

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(notify, "settings", dataclasses.replace(notify.settings, pushover_file=str(f)))
    assert notify.send("mail from ola@example.org", "file.pdf: imported", notify.QUIET)
    url, fields = sent[0]
    assert url == notify.API
    assert fields["token"] == "a1b2c3" and fields["user"] == "u9u8u7" and fields["priority"] == "-1"

    def broken(req, timeout):
        raise OSError("no network")
    monkeypatch.setattr(notify.urllib.request, "urlopen", broken)
    assert notify.send("sync failed") is False                     # never raises
    monkeypatch.setattr(notify, "settings", dataclasses.replace(notify.settings, pushover_file=""))
    assert notify.send("sync failed") is False
