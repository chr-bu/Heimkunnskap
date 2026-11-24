"""Synthetic trumf.no responses, framed as the site sends them: the receipt
list as a React Server Components payload (RSC: 1), a receipt as a full
HTML page with the payload in self.__next_f.push strings. Invented
purchases; no real member or store data."""

import json

MEMBER = "00000000-0000-0000-0000-000000000000"


def purchase(batch, when, partner, store, kr, bonus, kind="ACCRUAL", receipt=True):
    return {"batchId": batch, "belop": kr, "beskrivelse": store, "bonus": bonus,
            "bonusberegningTidspunkt": "$D" + when[:10] + "T00:00:00.000Z", "filterCategory": partner,
            "harKvittering": receipt, "kilde": "001", "medlemId": MEMBER, "partnerId": partner,
            "reklamasjonsStatus": "", "totaleBesparelser": 0, "transaksjonKategori": kind,
            "transaksjonsTidspunkt": "$D" + when}


PURCHASES = [
    purchase("2611200000000000000000000001", "2026-11-20T15:02:11.000Z", "KIWI", "KIWI Eksempelveien",
             312.4, 3.12),
    purchase("2611180000000000000000000002", "2026-11-18T09:40:00.000Z", "MENY", "MENY Eksempelsenteret",
             1045.0, 20.9),
    purchase("2611190000000000000000000003", "2026-11-19T10:00:00.000Z", "VIAT", "Eksempel Nettbutikk",
             899.0, 35.96, receipt=False),                        # a web shop: not groceries
    purchase("", "2026-11-15T00:00:00.000Z", "EB", "Overført til EuroBonus", 250, -250.0,
             kind="CONSUME", receipt=False),                      # transfers have no batchId
    purchase("", "2026-11-04T00:00:00.000Z", "EB", "Overført til EuroBonus", 200, -200.0,
             kind="CONSUME", receipt=False),
    purchase("", "2026-11-04T00:00:00.000Z", "EB", "Overført til EuroBonus", 200, -200.0,
             kind="CONSUME", receipt=False),                      # two alike the same day
]


def rsc_list(purchases=PURCHASES) -> str:
    """Line-framed id:json, as the receipt list comes with RSC: 1."""
    props = {"transactionsData": [{"month": "2026-11", "transactions": purchases}],
             "member": "$undefined"}
    return "\n".join([
        '0:{"P":null,"b":"build","c":["","trumf-profil","kvitteringer"]}',
        '1:I["chunks/1.js",["chunks/1.js"],"default"]',
        "9:" + json.dumps(["$", "$L1", None, props], separators=(",", ":")),
        '2:["$","div",null,{"children":"Kvitteringer"}]',
    ]) + "\n"


def line(n, product, kr, bonus, qty=1, unit="EA", ean="7000000000000", rules=None):
    return {"varelinjeGuid": f"guid-{n}", "ean": ean, "produktBeskrivelse": product, "antall": qty,
            "belop": kr, "bonus": bonus, "enhetsBonus": 0, "enhetsType": unit, "ukjentVare": False,
            "bonusProsent": 1, "momsProsent": 15,
            "bonusregler": rules or [{"bonusregelId": "R-1", "bonus": bonus,
                                      "bonusregelBeskrivelse": "Trumf grunnbonus"}],
            "besparelser": [], "reklamasjonsStatus": "", "reklamasjonsSum": 0}


def receipt_html(p, lines) -> str:
    """A receipt page: the payload split over two push calls, escaped."""
    details = {"batchId": p["batchId"], "kvitteringsId": "K1", "ordreId": "$undefined",
               "belop": p["belop"], "bonus": p["bonus"], "beskrivelse": p["beskrivelse"],
               "transaksjonsTidspunkt": p["transaksjonsTidspunkt"], "varelinjer": lines,
               "bonusreglerSum": [], "momsreglerSum": [], "besparelserSum": 0}
    flight = "a:" + json.dumps(["$", "$Lb", None, {"description": p["beskrivelse"],
                                                    "partnerId": p["partnerId"],
                                                    "purchaseDetails": details}],
                               separators=(",", ":")) + "\n"
    half = len(flight) // 2
    pushes = "".join(f"<script>self.__next_f.push([1,{json.dumps(part)}])</script>"
                     for part in (flight[:half], flight[half:]))
    return ("<!DOCTYPE html><html><head><title>Kvittering</title></head><body><div id=\"__next\">"
            "</div><script>(self.__next_f=self.__next_f||[]).push([0])</script>" + pushes
            + "</body></html>")


RECEIPTS = {
    PURCHASES[0]["batchId"]: [line(1, "Melk lett 1l", 24.9, 0.25, qty=2),
                              line(2, "Bananer", 32.5, 0.33, qty=1.234, unit="KG"),
                              line(3, "Kaffe filtermalt", 255.0, 2.54)],
    PURCHASES[1]["batchId"]: [line(1, "Laks filet", 1045.0, 20.9,
                                   rules=[{"bonusregelId": "R-2", "bonus": 20.9,
                                           "bonusregelBeskrivelse": "Dobbel bonus fisk"}])],
}


class FakeSite:
    """Stands in for trumf.Client: answers paths from the pages above."""

    def __init__(self, ended=False, list_text=None):
        self.jar = {"__Secure-authjs.session-token": "old"}
        self.changed = False
        self.ended = ended
        self.list_text = list_text
        self.paths = []

    def get(self, path, rsc=False):
        from heimkunnskap import trumf
        self.paths.append((path.split("?")[0], rsc))
        if path == "/api/auth/session":
            if self.ended:
                return "{}"
            self.jar["__Secure-authjs.session-token"] = "renewed"
            self.changed = True
            return json.dumps({"user": {}, "expires": "2027-11-23T20:44:00.000Z", "provider": "x",
                               "accessToken": "x", "idToken": "x", "isOrganization": False,
                               "memberId": "1", "accessTokenExpired": False})   # as trumf.no's
        if path == "/trumf-profil/kvitteringer":
            if not rsc:
                return "<!DOCTYPE html><html><body>no data in the HTML</body></html>"
            return self.list_text if self.list_text is not None else rsc_list()
        for p in PURCHASES:
            if path.startswith(f"/trumf-profil/kvitteringer/{p['batchId']}?"):
                return receipt_html(p, RECEIPTS[p["batchId"]])
        raise trumf.TrumfError(f"trumf.no answered 404 for {path}")
