import copy
import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location("validate_dashboard", Path(__file__).parents[1] / "scripts/validate_dashboard.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.master = [{"name_en": "Kisakihama"}, {"name_en": "nami-01170001"}]
        # UTC is still Sep 12, but Japan has crossed midnight.
        self.now = datetime(2026, 9, 12, 15, 1, tzinfo=timezone.utc)
        self.dashboard = {"points": [dict(p, tide_port="MG", tide_station={"port": "MG"}, slots=[
            {"timestamp_jst": f"2026-09-{day}T{hour:02d}:00:00+09:00", "htsgw_m": 0.0, "wind_ms": 0.0, "tide_m": 0.0}
            for day in range(13, 20) for hour in range(0, 24, 3)
        ]) for p in self.master]}

        for point in self.dashboard["points"]:
            point["tide_hourly"] = [{"timestamp_jst": f"2026-09-{day}T{hour:02d}:00:00+09:00", "tide_m": 0.0}
                for day in range(13, 20) for hour in range(24)]

    def test_sparse_tides_cannot_publish(self):
        self.dashboard["points"][0]["tide_hourly"].pop(160)
        with self.assertRaisesRegex(ValueError, "missing hourly tide"):
            module.validate_dashboard(self.dashboard, self.master, self.now)

    def test_future_missing_wave_or_wind_rejected(self):
        for field in ("htsgw_m", "wind_ms"):
            feed = copy.deepcopy(self.dashboard)
            feed["points"][0]["slots"][22][field] = None
            with self.assertRaises(ValueError):
                module.validate_dashboard(feed, self.master, self.now)

    def test_missing_evening_and_truncated_horizon_rejected(self):
        for slots in (self.dashboard["points"][0]["slots"][:-4], self.dashboard["points"][0]["slots"][:21] + self.dashboard["points"][0]["slots"][24:]):
            feed = copy.deepcopy(self.dashboard)
            feed["points"][0]["slots"] = slots
            with self.assertRaises(ValueError):
                module.validate_dashboard(feed, self.master, self.now)

    def test_complete_feed_including_zero_waves(self):
        module.validate_dashboard(self.dashboard, self.master, self.now)

    def test_miyazaki_only_cannot_overwrite_nationwide(self):
        self.dashboard["points"].pop()
        with self.assertRaisesRegex(ValueError, "Missing 1 nationwide"):
            module.validate_dashboard(self.dashboard, self.master, self.now)

    def test_existing_scoring_aliases_are_accepted(self):
        self.master.append({"name_en": "Takanabe"})
        self.dashboard["points"].append(dict(copy.deepcopy(self.dashboard["points"][0]), name_en="Kakuchi"))
        self.dashboard["points"][0]["name_en"] = "Kizakihama"
        module.validate_dashboard(self.dashboard, self.master, self.now)

    def test_alias_duplicates_are_rejected(self):
        self.dashboard["points"].append(dict(copy.deepcopy(self.dashboard["points"][0]), name_en="Kizakihama"))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            module.validate_dashboard(self.dashboard, self.master, self.now)

    def test_duplicate_ids_cannot_hide_missing_points(self):
        self.dashboard["points"].append(copy.deepcopy(self.dashboard["points"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            module.validate_dashboard(self.dashboard, self.master, self.now)

    def test_missing_or_invalid_wave_data_is_rejected(self):
        for wave in (None, float("nan"), float("inf"), -1, True):
            with self.subTest(wave=wave):
                self.dashboard["points"][1]["slots"][0]["htsgw_m"] = wave
                with self.assertRaisesRegex(ValueError, "missing current"):
                    module.validate_dashboard(self.dashboard, self.master, self.now)

    def test_previous_days_only_are_rejected(self):
        self.now = datetime(2026, 9, 14, 15, 1, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            module.validate_dashboard(self.dashboard, self.master, self.now)


if __name__ == "__main__":
    unittest.main()
