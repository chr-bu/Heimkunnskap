from datetime import date

from heimkunnskap import progress
from heimkunnskap.web import charts

CARDS = [("amex", "Amex Norway Reward"), ("dnb", "DNB Rewards"), ("sas", "MasterCard Reward Norway")]


def test_qualification_period():
    assert progress.qualification_period(2, 2026) == (date(2026, 2, 1), date(2027, 1, 31))
    assert progress.current_period(2, date(2026, 11, 26)) == (date(2026, 2, 1), date(2027, 1, 31))
    assert progress.current_period(2, date(2027, 1, 15)) == (date(2026, 2, 1), date(2027, 1, 31))
    assert progress.current_period(1, date(2026, 11, 26)) == (date(2026, 1, 1), date(2026, 12, 31))


def test_sources():
    assert progress.source_of("Level points from Amex Norway Reward", CARDS) == "amex"
    assert progress.source_of("Level Points from DNB Rewards", CARDS) == "dnb"
    assert progress.source_of("Level Points from MasterCard Reward", CARDS) == "sas"
    assert progress.source_of("MasterCard Reward Norway", CARDS) == "sas"
    assert progress.source_of("NorgesGruppen", CARDS) == "trumf"
    assert progress.source_of("Oslo - Paris", CARDS) == "flights"
    assert progress.source_of("EuroBonus Online Shopping, Health and beauty", CARDS) == "other"


def test_reached_and_projection():
    series = progress.cumulative([(date(2026, 1, 20), 60_000), (date(2026, 3, 2), 50_000),
                                  (date(2026, 5, 30), 120_000)])
    assert series[-1] == (date(2026, 5, 30), 230_000)
    assert progress.reached(series, 100_000) == date(2026, 3, 2)
    assert progress.reached(series, 500_000) is None
    start, end = date(2026, 1, 1), date(2026, 12, 31)
    assert round(progress.projection(181_000, start, end, date(2026, 6, 30))) == 365_000
    assert progress.projection(1000, start, end, date(2026, 1, 3)) is None        # too early
    assert progress.projection(1000, start, end, date(2027, 1, 3)) is None        # over


def test_progress_chart_draws_milestones_and_hover():
    series = [(date(2026, 3, 2), 110_000.0), (date(2026, 6, 1), 230_000.0)]
    svg = str(charts.progress(series, date(2026, 1, 1), date(2026, 12, 31), date(2026, 7, 1),
                              [{"value": 100_000, "reward": "10 000 level points", "reached": date(2026, 3, 2)},
                               {"value": 500_000, "reward": "10 000 level points", "reached": None}],
                              "kr", projected=460_000, cap=700_000, label="DNB"))
    assert svg.startswith('<svg class="viz"') and svg.endswith("</svg>")
    assert "✓ 100 000 kr" in svg and "500 000 kr" in svg and "no points above" in svg
    assert "At this pace: 460 000 kr" in svg
    assert "Jun 2026: 230 000 kr by the end of the month" in svg
    assert "style=" not in svg and "<script" not in svg                 # the page's CSP allows neither


def test_stacked_months():
    months = [date(2026, 1, 1), date(2026, 2, 1)]
    svg = str(charts.stacked_months(months, {"amex": [100, 200], "trumf": [0, 50]},
                                    progress.SOURCE_ORDER, names=progress.SOURCE_LABELS))
    assert "Feb 2026, Trumf: 50 points" in svg and "Feb 2026: 250 points" in svg
    assert 'class="viz-s1"' in svg and 'class="viz-s3"' in svg          # slot follows the source
    assert "style=" not in svg


def test_axis_scale():
    top, grid = charts.scale(440_000)
    assert top >= 440_000 and grid[1] in (100_000, 200_000) and grid[-1] == top
    assert charts.scale(73_000) == (80_000, [0, 20_000, 40_000, 60_000, 80_000])
