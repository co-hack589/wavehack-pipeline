import sys
import unittest
from pathlib import Path

import numpy as np
import xarray as xr

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from wavehack_pipeline.gfs_wave import resolve_ocean_grid


class OceanGridTests(unittest.TestCase):
    def dataset(self, water):
        values = np.full((7, 7), np.nan)
        for y, x in water:
            values[y, x] = 0.0
        return xr.Dataset({"swh": (("latitude", "longitude"), values)}, coords={
            "latitude": 33 + np.arange(-3, 4) / 6,
            "longitude": 130 + np.arange(-3, 4) / 6,
        })

    def test_nearest_ocean_zero_wave_is_valid(self):
        self.assertEqual(resolve_ocean_grid(self.dataset([(3, 3)]), 33, 130), (33, 130))

    def test_west_facing_coast_finds_ocean(self):
        self.assertEqual(resolve_ocean_grid(self.dataset([(3, 2)]), 33, 130), (33, 129.8333))

    def test_near_west_is_used_before_distant_east(self):
        self.assertEqual(resolve_ocean_grid(self.dataset([(3, 2), (3, 6)]), 33, 130), (33, 129.8333))

    def test_no_ocean_keeps_missing_nearest_grid(self):
        self.assertEqual(resolve_ocean_grid(self.dataset([]), 33, 130), (33, 130))


if __name__ == "__main__":
    unittest.main()
