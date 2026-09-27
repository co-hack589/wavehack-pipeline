import importlib.util
import json
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
from wavehack_pipeline.tide import JST, POINT_TO_PORT, TIDE_STATIONS, TideDay, tide_at, hourly_tide_series
spec = importlib.util.spec_from_file_location("fetch_tide", ROOT / "scripts/fetch_tide.py")
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)


class TideTests(unittest.TestCase):
    def test_hourly_series_keeps_every_hour_and_missing_source(self):
        day = date(2026, 10, 3)
        table = {day: TideDay(day, [-20, 0] + list(range(2,24)), [], [])}
        rows = hourly_tide_series(table, {day})
        self.assertEqual(len(rows), 24)
        self.assertEqual([x["tide_m"] for x in rows[:3]], [-0.2, 0, 0.02])
        self.assertEqual(rows[-1]["timestamp_jst"], "2026-10-03T23:00:00+09:00")
        self.assertTrue(all(x["tide_m"] is None for x in hourly_tide_series({}, {day})))

    def test_all_points_have_reviewed_stations(self):
        master = json.loads((ROOT / "data/all_points.json").read_text())
        self.assertEqual(len(master), 151)
        for p in master:
            self.assertIn(p["name"], TIDE_STATIONS)
        by_id = {s["point_id"]: s["port"] for s in TIDE_STATIONS.values()}
        for point, port in {"nami-01160007": "CS", "nami-01210014": "I4", "nami-02490003": "ZJ"}.items():
            self.assertEqual(by_id[point], port, "must not use opposite-side bay")
        self.assertEqual(POINT_TO_PORT["お倉ヶ浜"], "Z6")

    def test_missing_zero_negative_and_midnight(self):
        d = date(2026, 12, 31)
        day = TideDay(d, [0] * 23 + [-20], [], [])
        table = {d: day}
        self.assertEqual(tide_at(table, datetime(2026, 12, 31, 0, tzinfo=JST))[0], 0)
        self.assertEqual(tide_at(table, datetime(2026, 12, 31, 23, tzinfo=JST))[0], -.2)
        self.assertEqual(tide_at(table, datetime(2026, 12, 31, 23, 30, tzinfo=JST)), (None, None))
        table[date(2027, 1, 1)] = TideDay(date(2027, 1, 1), [20] * 24, [], [])
        self.assertEqual(tide_at(table, datetime(2026, 12, 31, 23, 30, tzinfo=JST))[0], 0)
        day.hourly_cm[0] = 999
        self.assertEqual(tide_at(table, datetime(2026, 12, 31, 0, tzinfo=JST)), (None, None))

    def test_year_boundary_fetches_both_years(self):
        dates = fetch.forecast_dates({"points": [{"slots": [
            {"timestamp_jst": "2026-12-31T23:00:00+09:00"},
            {"timestamp_jst": "2027-01-01T00:00:00+09:00"}]}]})
        def fake(year, port):
            return year
        def parse(year):
            return {d: TideDay(d, [0] * 24, [], []) for d in dates if d.year == year}
        with patch.object(fetch, "fetch_jma_tide", side_effect=fake) as request, patch.object(fetch, "parse_jma_text", side_effect=parse):
            table = fetch.fetch_tables(["MG"], dates)
            self.assertEqual({c.args[0] for c in request.call_args_list}, {2026, 2027})
            self.assertEqual(set(table["MG"]), dates)

    def test_partial_download_fails_instead_of_publishing(self):
        with patch.object(fetch, "fetch_jma_tide", return_value=""), patch.object(fetch, "parse_jma_text", return_value={}):
            with self.assertRaisesRegex(ValueError, "missing forecast dates"):
                fetch.fetch_tables(["MG"], {date(2026, 9, 13)})


if __name__ == "__main__":
    unittest.main()
