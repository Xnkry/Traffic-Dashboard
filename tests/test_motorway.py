import unittest
from unittest.mock import patch

from motorway import STUDY, motorway_payload, corridor_incidents
from collector import collection_command


class MotorwayTests(unittest.TestCase):
    def way(self, ref="A3", highway="motorway"):
        return {"type": "way", "id": 1, "tags": {"ref": ref, "highway": highway}, "geometry": [{"lon": -7.7, "lat": lat} for lat in [31.6, 31.7, 32.5, 33.5, 33.7]]}

    def test_only_main_route_and_bounded_vertices(self):
        result = motorway_payload({"elements": [self.way(), self.way("A4"), self.way(highway="motorway_link")]})
        self.assertEqual(len(result["elements"]), 1)
        self.assertEqual(len(result["elements"][0]["geometry"]), 3)

    def test_partial_import_rejected(self):
        with self.assertRaises(ValueError):
            motorway_payload({"elements": [], "remark": "timeout"})
        way = self.way()
        way["geometry"] = way["geometry"][2:4]
        with self.assertRaises(ValueError):
            motorway_payload({"elements": [way]})

    def test_incidents_filtered_deduplicated_and_failure_preserved(self):
        incident = {"geometry": {"type": "Point", "coordinates": [-7.7, 32.5]}, "properties": {"id": "one", "iconCategory": 1}}
        offroad = {"geometry": {"type": "Point", "coordinates": [-8, 32.5]}, "properties": {"id": "off", "iconCategory": 1}}
        roads = [{"geometry": [[-7.7,32.5], [-7.7,32.51]]}]*2
        with patch("road_ingest.fetch_incidents", return_value=(True, [incident,offroad])):
            ok, found = corridor_incidents(None, "test", roads)
        self.assertTrue(ok)
        self.assertEqual([i["properties"]["id"] for i in found], ["one"])
        with patch("road_ingest.fetch_incidents", return_value=(False, [])):
            self.assertFalse(corridor_incidents(None, "test", roads)[0])

    def test_worker_routes_to_road_collector(self):
        command = collection_command([], 20, STUDY)
        self.assertIn(STUDY, command)
        self.assertIn("--max-segments", command)

    def test_dashboard_speed_chart_and_incident_filter(self):
        from unittest.mock import patch
        import pandas as pd
        from streamlit.testing.v1 import AppTest
        from road_ingest import inventory_records
        from road_store import label_corridors
        from motorway import STUDY
        now=pd.Timestamp.now(tz="UTC")
        roads=label_corridors(pd.DataFrame(inventory_records({"elements":[{"type":"way","id":1,"tags":{"name":"A3","highway":"motorway"},"geometry":[{"lon":-7.7,"lat":32.5},{"lon":-7.7,"lat":32.503}]}]},STUDY)))
        history=pd.DataFrame([dict(id=1,segment_id=roads.iloc[0].id,observed_at=now,status="ok",confidence=.9,current_speed=60.,free_flow_speed=100.,speed_loss=40.,road_closure=False,geometry=roads.iloc[0].geometry,provider_key="test")])
        poll=dict(success=True, observed_at=now,payload=[dict(geometry=dict(type="Point",coordinates=[-7.7,32.5]),properties=dict(id="test",iconCategory=1))])
        with patch("motorway_dashboard.road_city_data",return_value=(roads,history,poll,{},pd.DataFrame())):
            app=AppTest.from_string('from motorway_dashboard import render; render("sqlite://", "Live database")').run(timeout=60)
            assert not app.exception, app.exception
            assert len(app.metric)==3
            app.toggle[0].set_value(True).run()
            assert not app.exception, app.exception
        
