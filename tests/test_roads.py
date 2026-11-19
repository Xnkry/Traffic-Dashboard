import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from network_inventory import pending_cities
from road_analysis import assess, baseline, distance_m, nearby_incidents, path_midpoint, split_path
from road_ingest import fetch_incidents, inventory_records, collect_city, resolve_area
from road_store import initialize, load_city, observations, polls, save_inventory, segments, label_corridors
from traffic_data import make_engine


PATH = [[-7.60, 33.58], [-7.59, 33.58]]
NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


def inventory():
    return inventory_records({"elements": [{"type": "way", "id": 123, "geometry": [{"lon": p[0], "lat": p[1]} for p in PATH], "tags": {"name": "Test avenue", "highway": "primary"}}]}, "Casablanca")


def reading(segment_id, at=NOW, loss=75):
    return dict(segment_id=segment_id, observed_at=at, status="ok", current_speed=15,
                free_flow_speed=60, speed_loss=loss, confidence=.9, road_closure=False,
                geometry=PATH, provider_key="segment-a")


class RoadAnalysisTests(unittest.TestCase):
    def test_nationwide_queue_resumes_without_starving_unattempted_cities(self):
        result = pending_cities(["Casablanca", "Rabat", "Fès", "Agadir"], {"Casablanca"}, {"Rabat": "2026-11-19", "Agadir": "2026-11-18"})
        self.assertEqual(result, ["Fès", "Agadir", "Rabat"])

    def test_split_keeps_geometry_and_limits_sample_lengths(self):
        parts = split_path(PATH, 400)
        self.assertEqual(parts[0][0], PATH[0])
        self.assertEqual(parts[-1][-1], PATH[-1])
        self.assertTrue(all(sum(distance_m(a, b) for a, b in zip(p, p[1:])) <= 400.01 for p in parts))
        self.assertAlmostEqual(path_midpoint(PATH)[0], -7.595)

    def test_baseline_requires_four_prior_dates_not_many_samples(self):
        current = reading("r")
        old = [reading("r", NOW - timedelta(weeks=i), 65) for i in range(1, 5)]
        self.assertEqual(baseline(pd.DataFrame(old), "r", NOW, "segment-a"), (65, 4))
        self.assertEqual(baseline(pd.DataFrame(old[:1] * 20), "r", NOW, "segment-a"), (None, 1))
        self.assertEqual(baseline(pd.DataFrame(old), "r", NOW, "other"), (None, 0))
        self.assertEqual(baseline(pd.DataFrame([current]), "r", NOW, "segment-a"), (None, 0))

    def test_failed_feed_and_missing_speed_never_mean_clear(self):
        roads = pd.DataFrame(inventory())
        result = assess(roads, pd.DataFrame(), None, NOW)
        self.assertTrue(result.condition.eq("Unobserved").all())
        history = pd.DataFrame([reading(roads.iloc[0].id)])
        result = assess(roads, history, {"success": False, "observed_at": NOW, "payload": []}, NOW)
        self.assertIn("unavailable", result.iloc[0].evidence)
        self.assertEqual(result.iloc[0].condition, "Heavy")
        stale = assess(roads, history, None, NOW + timedelta(hours=1))
        self.assertEqual(stale.iloc[0].condition, "Stale")

    def test_accident_is_nearby_evidence_not_causal_verdict(self):
        roads = pd.DataFrame(inventory())
        incident = {"geometry": {"type": "Point", "coordinates": [-7.595, 33.58]}, "properties": {"id": "a", "iconCategory": 1}}
        history = pd.DataFrame([reading(roads.iloc[0].id)])
        result = assess(roads, history, {"success": True, "observed_at": NOW, "payload": [incident]}, NOW)
        self.assertEqual(result.iloc[0].accident_count, 1)
        self.assertEqual(result.iloc[0].evidence, "Nearby report: Accident")
        far = {**incident, "geometry": {"type": "Point", "coordinates": [-7.5, 33.58]}}
        self.assertEqual(nearby_incidents(PATH, [far]), [])

    def test_recurring_and_unusual_patterns_are_distinct(self):
        roads = pd.DataFrame(inventory())
        segment_id = roads.iloc[0].id
        poll = {"success": True, "observed_at": NOW, "payload": []}
        old = [reading(segment_id, NOW - timedelta(weeks=i), 65) for i in range(1, 5)]
        current = reading(segment_id, NOW, 75)
        result = assess(roads, pd.DataFrame([*old, current]), poll, NOW)
        self.assertIn("recurring", result.iloc[0].evidence)
        for row in old:
            row["speed_loss"] = 10
        result = assess(roads, pd.DataFrame([*old, current]), poll, NOW)
        self.assertIn("Unusual", result.iloc[0].evidence)

    def test_malformed_incident_response_is_unknown(self):
        session = Mock()
        session.get.return_value.json.return_value = {"incidents": [{"properties": {"id": "a", "iconCategory": 1}, "geometry": {"type": "Point", "coordinates": [999, 30]}}]}
        self.assertEqual(fetch_incidents(session, "test", [0, 0, 1, 1]), (False, []))
        session.get.return_value.json.return_value = {"incidents": []}
        self.assertEqual(fetch_incidents(session, "test", [0, 0, 1, 1]), (True, []))


    @patch("road_ingest.overpass")
    def test_administrative_boundary_match_and_ambiguity(self, overpass):
        overpass.return_value = {"elements": [
            {"id": 3604072985, "tags": {"name": "Casablanca الدار البيضاء", "name:en": "Casablanca", "admin_level": "8"}},
            {"id": 3600000001, "tags": {"name": "Casablanca region", "admin_level": "6"}},
        ]}
        self.assertEqual(resolve_area("Casablanca", 33.57, -7.59)[0], 3604072985)
        overpass.return_value["elements"].append({"id": 3600000002, "tags": {"name": "Casablanca", "admin_level": "8"}})
        with self.assertRaises(ValueError):
            resolve_area("Casablanca", 33.57, -7.59)


class RoadStorageTests(unittest.TestCase):
    def test_reimport_preserves_history_and_empty_import_is_rejected(self):
        engine = make_engine("sqlite:///:memory:")
        records = inventory()
        save_inventory(engine, "Casablanca", [-8, 33, -7, 34], 8, records)
        with engine.begin() as conn:
            conn.execute(observations.insert().values(**reading(records[0]["id"])))
        save_inventory(engine, "Casablanca", [-8, 33, -7, 34], 8, records)
        with self.assertRaises(ValueError):
            save_inventory(engine, "Casablanca", [-8, 33, -7, 34], 8, [])
        roads, history, poll, area = load_city(engine, "Casablanca", days=365)
        self.assertEqual(len(roads), len(records))
        self.assertEqual(len(history), 1)
        self.assertIsNone(poll)
        engine.dispose()

    def test_disconnected_same_name_roads_are_not_merged(self):
        records = inventory()
        distant = {**records[0], "id": "far", "geometry": [[-7.5, 33.58], [-7.49, 33.58]]}
        labeled = label_corridors(pd.DataFrame([*records, distant]))
        self.assertEqual(labeled.corridor_id.nunique(), 2)
        self.assertEqual(labeled.iloc[0].corridor_id, labeled.iloc[1].corridor_id)
        self.assertNotEqual(labeled.iloc[0].corridor_label, labeled.iloc[-1].corridor_label)

    def test_partial_osm_response_is_rejected(self):
        with self.assertRaises(ValueError):
            inventory_records({"remark": "runtime timeout", "elements": []}, "Casablanca")


class RoadDashboardTests(unittest.TestCase):
    def test_demo_does_not_fabricate_road_accidents(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "dashboard.py"), default_timeout=30).run()
        app.sidebar.radio[0].set_value("Avenue intelligence").run()
        self.assertFalse(app.exception)
        self.assertTrue(any("Synthetic city samples are excluded" in item.value for item in app.info))


if __name__ == "__main__":
    unittest.main()
