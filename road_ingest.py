import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import unicodedata

import requests
import pandas as pd
from sqlalchemy import select, text

from city_catalog import catalog
from ingest import api_session
from road_analysis import distance_m, path_midpoint, point_line_distance, split_path, valid_path
from road_store import areas, initialize, observations, polls, save_inventory, segments, label_corridors
from traffic_data import database_url, make_engine, validate_flow


INCIDENT_URL = "https://api.tomtom.com/traffic/services/5/incidentDetails"
FIELDS = "{incidents{type,geometry{type,coordinates},properties{id,iconCategory,events{description},startTime,endTime,from,to,length,delay,roadNumbers}}}"
logger = logging.getLogger("atlas.roads")


def inventory_records(payload, city):
    if not isinstance(payload, dict) or payload.get("remark") or not isinstance(payload.get("elements"), list):
        raise ValueError("Incomplete or invalid OSM response; inventory preserved")
    records = []
    for way in payload["elements"]:
        if way.get("type") != "way":
            continue
        path = [[p["lon"], p["lat"]] for p in way.get("geometry", [])]
        if not valid_path(path):
            raise ValueError("Invalid OSM geometry; inventory preserved")
        tags = way.get("tags", {})
        for index, part in enumerate(split_path(path)):
            length = sum(distance_m(a, b) for a, b in zip(part, part[1:]))
            if length < 5:
                continue
            lon, lat = path_midpoint(part)
            version = hashlib.sha256(json.dumps([part, tags], sort_keys=True).encode()).hexdigest()[:16]
            records.append({"id": f"{city}/{way['id']}/{index}/{version}", "city": city,
                "name": tags.get("name", tags.get("ref", f"Unnamed road · OSM {way['id']}")),
                "osm_id": str(way["id"]), "road_class": tags.get("highway", "unknown"),
                "oneway": tags.get("oneway", "unknown"), "geometry": part, "lat": lat, "lon": lon,
                "length_m": length, "active": True, "last_attempt": None})
    return records


def overpass(query):
    response = requests.get("https://overpass-api.de/api/interpreter", params={"data": query},
        headers={"User-Agent": "AtlasTrafficResearch/1.0"}, timeout=(10, 150))
    response.raise_for_status()
    payload = response.json()
    if payload.get("remark") or not isinstance(payload.get("elements"), list):
        raise ValueError("Incomplete Overpass response; previous inventory preserved")
    return payload


def normalized_name(name):
    return "".join(c for c in unicodedata.normalize("NFKD", name).casefold() if c.isalnum())


def resolve_area(city, lat, lon):
    query = f'[out:json][timeout:45];is_in({lat},{lon});area._["boundary"="administrative"]["admin_level"~"^(5|6|7|8|9)$"];out tags;'
    payload = overpass(query)
    aliases = {normalized_name(city)}
    aliases.update({"Fès": ["fes", "fez"], "Marrakech": ["marrakesh"], "Tangier": ["tanger", "tangier"]}.get(city, []))
    candidates = []
    for element in payload["elements"]:
        tags = element.get("tags", {})
        names = [tags.get(k, "") for k in ("name", "name:fr", "name:en", "alt_name", "official_name")]
        if any(alias in normalized_name(name) for alias in aliases for name in names):
            candidates.append(element)
    if not candidates:
        raise ValueError("No unambiguous city boundary found. Supply --area-id from OpenStreetMap or explicitly use --radius-km for a bounded study.")
    candidates.sort(key=lambda e: int(e.get("tags", {}).get("admin_level", 0)), reverse=True)
    best = candidates[0]
    if len(candidates) > 1 and candidates[1].get("tags", {}).get("admin_level") == best.get("tags", {}).get("admin_level"):
        raise ValueError("Multiple city boundaries match; specify --area-id explicitly")
    return best["id"], best.get("tags", {}).get("name", city)


def import_city(engine, city, radius_km=None, area_id=None, network_file=None):
    center = catalog()[lambda x: x.city == city][["lat", "lon"]].mean()
    scope, boundary_name, bbox = "administrative", city, None
    if network_file:
        payload = json.loads(Path(network_file).read_text())
        scope = "file"
        if payload.get("type") == "FeatureCollection":
            elements = []
            for index, feature in enumerate(payload["features"]):
                props, geometry = feature.get("properties", {}), feature.get("geometry", {})
                if geometry.get("type") != "LineString" or not props.get("highway"):
                    continue
                elements.append({"type": "way", "id": props.get("osm_id", props.get("@id", feature.get("id", index))),
                    "tags": props, "geometry": [{"lon": p[0], "lat": p[1]} for p in geometry["coordinates"]]})
            payload = {"elements": elements}
    else:
        if radius_km is not None:
            scope = "bbox"
            dy = radius_km / 111.32
            dx = dy / math.cos(math.radians(center.lat))
            bbox = [float(center.lon - dx), float(center.lat - dy), float(center.lon + dx), float(center.lat + dy)]
            selector = f"({bbox[1]},{bbox[0]},{bbox[3]},{bbox[2]})"
        else:
            if area_id is None:
                area_id, boundary_name = resolve_area(city, center.lat, center.lon)
            selector = f"(area:{int(area_id)})"
        kinds = "motorway|motorway_link|trunk|trunk_link|primary|primary_link|secondary|secondary_link|tertiary|tertiary_link|residential|unclassified|living_street|service"
        query = f'[out:json][timeout:120][maxsize:67108864];way["highway"~"^({kinds})$"]["access"!~"^(private|no)$"]["area"!="yes"]{selector};out geom;'
        payload = overpass(query)
    records = inventory_records(payload, city)
    if bbox is not None:
        records = [r for r in records if bbox[0] <= r["lon"] <= bbox[2] and bbox[1] <= r["lat"] <= bbox[3]]
    if not records:
        raise ValueError("No road samples returned; previous inventory preserved")
    if bbox is None:
        points = [p for road in records for p in road["geometry"]]
        bbox = [min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)]
    save_inventory(engine, city, bbox, radius_km or 0, records, scope, str(area_id) if area_id else None, boundary_name)
    logger.info("Imported %s road samples across %s road-name groups for %s (%s)", len(records), len({r["name"] for r in records}), city, scope)
    return len(records)


def fetch_incidents(session, key, bbox):
    try:
        response = session.get(INCIDENT_URL, params={"key": key, "bbox": ",".join(map(str, bbox)),
            "fields": FIELDS, "language": "en-GB", "timeValidityFilter": "present"}, timeout=(5, 25))
        response.raise_for_status()
        payload = response.json()
        incidents = payload["incidents"]
        if not isinstance(incidents, list):
            raise ValueError("Invalid incidents")
        for incident in incidents:
            props, geometry = incident["properties"], incident["geometry"]
            coords = geometry["coordinates"]
            if geometry["type"] == "Point":
                coords = [coords]
            if geometry["type"] not in {"Point", "LineString"} or not valid_path(coords, 1) or not props.get("id") or not isinstance(props.get("iconCategory"), int):
                raise ValueError("Invalid incident")
        return True, incidents
    except (requests.RequestException, KeyError, TypeError, ValueError):
        logger.warning("Incident feed unavailable; not interpreting failure as zero accidents")
        return False, []



def fetch_road_flow(session, key, lat, lon):
    try:
        response = session.get("https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/16/json",
            params={"key": key, "point": f"{lat},{lon}", "unit": "KMPH", "openLr": "true"}, timeout=(5, 20))
        response.raise_for_status()
        payload = response.json()["flowSegmentData"]
        return {**validate_flow(payload), "raw_payload": payload}
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return None


def collect_city(engine, city, limit, key, avenue=None, corridor=None):
    initialize(engine)
    with engine.connect() as conn:
        area = conn.execute(select(areas).where(areas.c.city == city)).mappings().first()
        if not area:
            raise ValueError(f"Import the road network for {city} first")
        query = select(segments).where(segments.c.city == city, segments.c.active.is_(True))
        if avenue:
            query = query.where(segments.c.name == avenue)
        if corridor:
            inventory = pd.DataFrame(conn.execute(query).mappings().all())
            labeled = label_corridors(inventory)
            ids = labeled.loc[labeled.corridor_id == corridor, "id"].tolist() if not labeled.empty else []
            query = query.where(segments.c.id.in_(ids))
        roads = conn.execute(query.order_by(segments.c.last_attempt.asc().nullsfirst(), segments.c.id).limit(limit)).mappings().all()
        if not roads:
            raise ValueError("No active samples match this city/avenue")
    failed = 0
    with api_session() as session:
        from motorway import STUDY, corridor_incidents
        if city == STUDY:
            with engine.connect() as conn:
                network = conn.execute(select(segments).where(segments.c.city == city, segments.c.active.is_(True))).mappings().all()
            success, incidents = corridor_incidents(session, key, network)
        else:
            success, incidents = fetch_incidents(session, key, area["bbox"])
        with engine.begin() as conn:
            conn.execute(polls.insert().values(city=city, observed_at=datetime.now(timezone.utc), success=success, bbox=area["bbox"], payload=incidents))
        for road in roads:
            flow = fetch_road_flow(session, key, road["lat"], road["lon"])
            now = datetime.now(timezone.utc)
            value = dict(segment_id=road["id"], observed_at=now, status="unavailable")
            if flow:
                payload = flow["raw_payload"]
                try:
                    path = [[p["longitude"], p["latitude"]] for p in payload["coordinates"]["coordinate"]]
                    if not valid_path(path):
                        raise ValueError("Invalid flow geometry")
                    match_distance = point_line_distance([road["lon"], road["lat"]], path)
                    value.update({k: flow[k] for k in ("current_speed", "free_flow_speed", "confidence", "road_closure")})
                    value.update(status="ok" if match_distance <= 75 else "mismatch", geometry=path,
                        provider_key=payload.get("openlr") or hashlib.sha256(json.dumps(path).encode()).hexdigest(),
                        speed_loss=100 * (1 - min(flow["congestion_ratio"], 1)),
                        match_distance_m=match_distance, raw_payload=payload)
                except (KeyError, TypeError, ValueError):
                    value["status"] = "invalid_geometry"
            if value["status"] != "ok":
                failed += 1
            with engine.begin() as conn:
                conn.execute(observations.insert().values(**value))
                conn.execute(segments.update().where(segments.c.id == road["id"]).values(last_attempt=now))
    logger.info("%s: %s attempted road samples, %s unavailable/mismatched; incident feed %s", city, len(roads), failed, "available" if success else "unavailable")
    return int(failed > 0 or not success)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Import road geometry or collect avenue flow and incident evidence")
    parser.add_argument("action", choices=["import", "collect", "init"])
    parser.add_argument("--city")
    parser.add_argument("--corridor", help="Limit collection to a connected corridor ID shown in the dashboard")
    parser.add_argument("--avenue", help="Collect only this exact imported road name")
    parser.add_argument("--radius-km", type=float, help="Explicitly use a bounded study instead of a city boundary")
    parser.add_argument("--area-id", type=int, help="Verified Overpass area ID for the city/commune")
    parser.add_argument("--network-file", type=Path, help="Local Overpass JSON or OSM road GeoJSON for bulk/offline import")
    parser.add_argument("--max-segments", type=int, default=50)
    args = parser.parse_args(argv)
    if (args.radius_km is not None and not 1 <= args.radius_km <= 30) or not 1 <= args.max_segments <= 10000:
        parser.error("Use radius 1–30 km and max-segments 1–10000")
    if sum(x is not None for x in (args.radius_km, args.area_id, args.network_file)) > 1:
        parser.error("Choose only one of --radius-km, --area-id, or --network-file")
    if args.area_id is not None and args.area_id <= 0:
        parser.error("Area ID must be positive")
    from motorway import STUDY, import_motorway
    if args.action != "init" and args.city not in set(catalog().city) | {STUDY}:
        parser.error("Specify a city from the catalog")
    key = os.getenv("TOMTOM_API_KEY")
    if args.action == "collect" and not key:
        parser.error("Set TOMTOM_API_KEY")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    engine = make_engine(database_url())
    try:
        initialize(engine)
        with engine.connect() as lock:
            locked = engine.dialect.name == "postgresql"
            if locked and not lock.scalar(text("SELECT pg_try_advisory_lock(684219038)")):
                logger.info("Another road import or collector is running; skipped")
                return 0
            try:
                if args.action == "init":
                    return 0
                if args.action == "import":
                    if args.city == STUDY:
                        if args.radius_km is not None or args.area_id is not None:
                            raise ValueError("Motorway study uses its fixed corridor bounds")
                        count = import_motorway(engine, args.network_file)
                        logger.info("Imported %s motorway samples", count)
                    else:
                        import_city(engine, args.city, args.radius_km, args.area_id, args.network_file)
                    return 0
                return collect_city(engine, args.city, args.max_segments, key, args.avenue, args.corridor)
            finally:
                if locked:
                    lock.execute(text("SELECT pg_advisory_unlock(684219038)"))
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
