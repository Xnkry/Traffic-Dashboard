import json
from pathlib import Path

from road_analysis import nearby_incidents

STUDY = "Casablanca–Marrakech motorway"
BBOX = [-8.2, 31.68, -7.2, 33.58]
QUERY = '[out:json][timeout:120];way["highway"="motorway"]["ref"~"(^|;) *A3(;|$)"](31.68,-8.2,33.58,-7.2);out geom;'


def motorway_payload(payload):
    if payload.get("remark") or not isinstance(payload.get("elements"), list):
        raise ValueError("Incomplete motorway inventory")
    ways = []
    for way in payload["elements"]:
        tags = way.get("tags", {})
        if way.get("type") != "way" or tags.get("highway") != "motorway" or "A3" not in [s.strip() for s in tags.get("ref", "").split(";")]:
            continue
        points = way.get("geometry", [])
        if len(points) < 2:
            raise ValueError("Missing motorway geometry")
        parts, part = [], []
        for point in points:
            inside = BBOX[0] <= point["lon"] <= BBOX[2] and BBOX[1] <= point["lat"] <= BBOX[3]
            if inside:
                part.append(point)
            else:
                if len(part) > 1:
                    parts.append(part)
                part = []
        if len(part) > 1:
            parts.append(part)
        for index, part in enumerate(parts):
            ways.append({**way, "id": f"{way['id']}-{index}", "geometry": part})
    if not ways:
        raise ValueError("No A3 main carriageway geometry found; inventory preserved")
    lats = [p["lat"] for way in ways for p in way["geometry"]]
    if min(lats) > 31.8 or max(lats) < 33.4:
        raise ValueError("Inventory does not span both corridor endpoints")
    return {"elements": ways}


def import_motorway(engine, network_file=None):
    from road_ingest import overpass, inventory_records
    from road_store import save_inventory
    payload = json.loads(Path(network_file).read_text()) if network_file else overpass(QUERY)
    records = inventory_records(motorway_payload(payload), STUDY)
    save_inventory(engine, STUDY, BBOX, 0, records, "motorway", boundary_name="A3 main carriageways · bounded Casablanca–Marrakech study")
    return len(records)


def corridor_incidents(session, key, roads):
    from road_ingest import fetch_incidents
    tiles = {}
    for road in roads:
        for lon, lat in road["geometry"]:
            tile = (int((lon - BBOX[0]) / .25), int((lat - BBOX[1]) / .25))
            tiles.setdefault(tile, []).append(road["geometry"])
    found, complete = {}, True
    for (x, y), paths in tiles.items():
        west, south = BBOX[0] + x * .25, BBOX[1] + y * .25
        ok, incidents = fetch_incidents(session, key, [west-.002, south-.002, west+.252, south+.252])
        complete = complete and ok
        for path in paths:
            for incident in nearby_incidents(path, incidents, threshold=60):
                found[incident["properties"]["id"]] = incident
    return complete, list(found.values())
