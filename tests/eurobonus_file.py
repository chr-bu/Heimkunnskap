"""A synthetic EuroBonus export, as sas.no writes it. No real values."""
import io


def export(rows=None) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Transactions"
    ws.append(["Date", "Activity", "Bonus Points", "Level Points"])
    for r in rows if rows is not None else [
            ("2026-11-23", "DNB Rewards | Extra Points", 1650, None),
            ("2026-11-20", "NorgesGruppen | Extra Points", 2700, None),
            ("2026-11-08", "Level Points from MasterCard Reward | Mastercard Status Points", None, 500),
            ("2026-11-04", "Amex Norway Reward | Extra Points", 200, None),
            ("2026-11-04", "Amex Norway Reward | Extra Points", 200, None),
            ("2026-11-02", "Oslo - Bangkok | ", -190000, None),
            ("2026-10-23", "DNB Rewards | Extra Points", 1500, None),
            ("2026-10-23", "Level Points from DNB Rewards | Status Points", None, 10000),
            ("2026-10-10", "EuroBonus Online Shopping, Books | Norli | Extra Points", 349, 70),
            ("2017-06-23", "Oslo - London | Basic Points", 500, 500)]:
        ws.append(list(r))
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
