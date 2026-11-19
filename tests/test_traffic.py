import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from streamlit.testing.v1 import AppTest

from city_catalog import catalog
from ingest import fetch_traffic, save_batch
from traffic_data import (
    batches, cities, demo_data, initialize_database, load_readings,
    locations, make_engine, readings, snapshot, validate_flow,
)
from traffic_views import map_data


class TrafficDataTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine("sqlite:///:memory:")
        initialize_database(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_catalog_seed_is_idempotent(self):
        initialize_database(self.engine)
        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(select(func.count()).select_from(cities)), 12)
            self.assertEqual(conn.scalar(select(func.count()).select_from(locations)), 36)
        self.assertEqual(catalog().location_key.nunique(), 36)

    def test_invalid_provider_values_are_rejected(self):
        for flow in [
            {"currentSpeed": 10, "freeFlowSpeed": 0},
            {"currentSpeed": -1, "freeFlowSpeed": 30},
            {"currentSpeed": float("nan"), "freeFlowSpeed": 30},
            {"currentSpeed": 10, "freeFlowSpeed": float("inf")},
            {"currentSpeed": 10, "freeFlowSpeed": 30, "confidence": 1.2},
            {"currentSpeed": 10, "freeFlowSpeed": 30, "roadClosure": "false"},
        ]:
            with self.subTest(flow=flow), self.assertRaises(ValueError):
                validate_flow(flow)
        valid = validate_flow({"currentSpeed": 0, "freeFlowSpeed": 40, "roadClosure": True})
        self.assertEqual(valid["congestion_ratio"], 0)

    def test_bad_api_payload_does_not_abort_batch(self):
        session = Mock()
        session.get.return_value.json.return_value = {"flowSegmentData": {"currentSpeed": 0, "freeFlowSpeed": 0}}
        self.assertIsNone(fetch_traffic(session, "test-key", 33, -7))

    def test_demo_is_repeatable_and_labeled(self):
        now = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
        a, b = demo_data(now, days=1), demo_data(now, days=1)
        pd.testing.assert_frame_equal(a, b)
        self.assertEqual(len(a), 36 * 96)
        self.assertTrue(a.source.eq("demo").all())
        self.assertTrue(a.congestion_pct.between(0, 100).all())

    def test_upsert_source_isolation_and_atomic_rollback(self):
        now = datetime.now(timezone.utc)
        records = demo_data(now, days=1).head(3).to_dict("records")
        self.assertEqual(save_batch(self.engine, records, "one", now, "demo", 3, 0), 3)
        self.assertEqual(save_batch(self.engine, records, "two", now, "demo", 3, 0), 0)
        self.assertEqual(len(load_readings(self.engine, "demo")), 3)
        self.assertTrue(load_readings(self.engine, "tomtom").empty)
        invalid = [{**records[0], "free_flow_speed": 0, "recorded_at": now}]
        with self.assertRaises(IntegrityError):
            save_batch(self.engine, invalid, "invalid", now, "demo", 1, 0)
        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(select(func.count()).select_from(batches)), 2)

    def test_snapshot_never_includes_future_readings(self):
        frame = demo_data(days=1)
        at = frame.recorded_at.min() + pd.Timedelta(hours=2)
        snap = snapshot(frame, at)
        self.assertEqual(len(snap), 36)
        self.assertTrue(snap.recorded_at.le(at).all())
        points, national = map_data(snap, at)
        self.assertTrue(national)
        self.assertEqual(len(points), 12)
        points, national = map_data(snap[snap.city == "Casablanca"], at)
        self.assertFalse(national)
        self.assertEqual(len(points), 3)


class DashboardTests(unittest.TestCase):
    def app(self):
        return AppTest.from_file(str(Path(__file__).resolve().parents[1] / "dashboard.py"), default_timeout=20).run()

    def test_overview_city_filter_and_empty_selection(self):
        app = self.app()
        self.assertFalse(app.exception)
        self.assertEqual(app.metric[3].value, "36 / 36")
        app.sidebar.multiselect[0].set_value(["Casablanca"]).run()
        self.assertFalse(app.exception)
        self.assertEqual(app.metric[3].value, "3 / 3")
        app.sidebar.multiselect[0].set_value([]).run()
        self.assertFalse(app.exception)
        self.assertTrue(app.info)

    def test_patterns_and_data_explorer(self):
        app = self.app()
        app.sidebar.radio[0].set_value("Patterns").run()
        self.assertFalse(app.exception)
        app.segmented_control[0].set_value("Road speed").run()
        self.assertFalse(app.exception)
        app.sidebar.radio[0].set_value("Data studio").run()
        self.assertFalse(app.exception)
        app.text_input[0].set_value("no-such-district").run()
        self.assertEqual(len(app.dataframe[0].value), 0)
        app.segmented_control[0].set_value("City catalog").run()
        self.assertEqual(len(app.dataframe[0].value), 36)
        app.segmented_control[0].set_value("Pipeline health").run()
        self.assertFalse(app.exception)

    def test_replay_and_map_modes(self):
        app = self.app()
        app.segmented_control[0].set_value("2D points").run()
        app.select_slider[1].set_value(0).run()
        self.assertFalse(app.exception)
        self.assertTrue(app.get("deck_gl_json_chart"))


if __name__ == "__main__":
    unittest.main()
