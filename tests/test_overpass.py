import os
import unittest
from unittest.mock import Mock, MagicMock, patch

import requests
from road_ingest import overpass, main, MissingInventory


class OverpassTests(unittest.TestCase):
    @patch.dict(os.environ, {"OVERPASS_URLS": "https://first.example/api,https://second.example/api", "OVERPASS_READ_TIMEOUT": "20"})
    def test_timeout_falls_back_and_reports_complete_payload(self):
        response = Mock()
        response.json.return_value = {"elements": [{"id": 1}]}
        with patch("road_ingest.requests.get", side_effect=[requests.Timeout(), response]) as get:
            self.assertEqual(overpass("[timeout:120];node;out;"), response.json.return_value)
            self.assertEqual(get.call_count, 2)
            self.assertEqual(get.call_args.kwargs["timeout"], (5, 20))
            self.assertIn("[timeout:15]", get.call_args.kwargs["params"]["data"])

    @patch.dict(os.environ, {"OVERPASS_URLS": "https://first.example/api", "OVERPASS_READ_TIMEOUT": "20"})
    def test_partial_response_is_not_used(self):
        response = Mock()
        response.json.return_value = {"elements": [], "remark": "runtime timeout"}
        with patch("road_ingest.requests.get", return_value=response):
            with self.assertRaisesRegex(ValueError, "Existing inventory is unchanged"):
                overpass("[timeout:120];node;out;")

    @patch.dict(os.environ, {"TOMTOM_API_KEY": "test"})
    def test_missing_inventory_has_distinct_exit_status(self):
        engine = MagicMock()
        engine.dialect.name = "sqlite"
        with patch("road_ingest.make_engine", return_value=engine), patch("road_ingest.initialize"), patch("road_ingest.collect_city", side_effect=MissingInventory("Import first")):
            self.assertEqual(main(["collect", "--city", "Casablanca"]), 2)
        engine.dispose.assert_called_once()

    @patch.dict(os.environ, {"TOMTOM_API_KEY": "test"})
    @patch("collector.signal.signal")
    @patch("collector.subprocess.Popen")
    def test_worker_stops_for_missing_inventory_without_once(self, popen, signal):
        from collector import main as collect
        child = popen.return_value.__enter__.return_value
        child.poll.return_value = 2
        child.returncode = 2
        self.assertEqual(collect(["--road-city", "Casablanca"]), 2)
        popen.assert_called_once()
