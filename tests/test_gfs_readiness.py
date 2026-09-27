import sys
import unittest
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from wavehack_pipeline import gfs_wave as gfs

class ReadinessTests(unittest.TestCase):
    def test_latest_incomplete_run_falls_back_to_complete_run(self):
        session = Mock()
        session.head.side_effect = [SimpleNamespace(status_code=404), SimpleNamespace(status_code=404), SimpleNamespace(status_code=200)]
        with patch.object(gfs, '_session', return_value=session):
            selected = gfs.latest_run_base(required_fhour=168)
        urls = [c.args[0] for c in session.head.call_args_list]
        self.assertTrue(all('f168.grib2' in url for url in urls))
        self.assertEqual(urls[-1], gfs._grib_url(selected,168))
        self.assertNotEqual(urls[0],urls[-1])

    def test_no_complete_run_fails(self):
        session=Mock()
        session.head.return_value.status_code=404
        with patch.object(gfs,'_session',return_value=session), self.assertRaises(RuntimeError):
            gfs.latest_run_base(max_lookback=2)

    def test_any_slot_download_failure_stops_publication(self):
        dataset=Mock()
        dataset.__getitem__=Mock(return_value=Mock())
        with patch.object(gfs,'_download',side_effect=[Path('mock'),RuntimeError('404')]), patch.object(gfs,'_open_two_datasets',return_value=(dataset,Mock())):
            with self.assertRaisesRegex(RuntimeError,'refusing to publish'):
                gfs.fetch_forecast([],run=datetime(2026,9,26,18,tzinfo=timezone.utc),fhours=[60],max_workers=1)
