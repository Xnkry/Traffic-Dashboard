import argparse
import logging
import os
import uuid
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from sqlalchemy import select
from urllib3.util.retry import Retry

from city_catalog import catalog
from traffic_data import (
    batches, cities, database_url, demo_data, initialize_database,
    insert_ignore, locations, make_engine, readings, validate_flow,
)


URL = "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"
logger = logging.getLogger("trafficapp.ingest")


def api_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"], respect_retry_after_header=True)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_traffic(session, api_key, lat, lon):
    try:
        response = session.get(URL, params={"point": f"{lat},{lon}", "key": api_key, "unit": "KMPH"}, timeout=(5, 20))
        response.raise_for_status()
        payload = response.json()["flowSegmentData"]
        return {**validate_flow(payload), "raw_payload": payload}
    except (requests.RequestException, ValueError, KeyError, TypeError):
        logger.warning("No valid reading at %.4f, %.4f", lat, lon)
        return None


def save_batch(engine, records, run_id, started_at, source, requested, failed):
    with engine.begin() as connection:
        connection.execute(batches.insert().values(id=run_id, started_at=started_at,
            finished_at=datetime.now(timezone.utc), source=source,
            requested=requested, succeeded=len(records), failed=failed))
        lookup = {(row.city, row.name): row.id for row in connection.execute(
            select(locations.c.id, cities.c.name.label("city"), locations.c.name)
            .select_from(locations.join(cities)))}
        inserted = 0
        for record in records:
            value = {key: record[key] for key in (
                "recorded_at", "current_speed", "free_flow_speed", "congestion_ratio",
                "confidence", "road_closure")}
            value.update(location_id=lookup[(record["city"], record["location"])],
                source=source, run_id=run_id, raw_payload=record.get("raw_payload"))
            result = insert_ignore(connection, readings, value, ["location_id", "recorded_at", "source"])
            inserted += result.rowcount
    return inserted


def main(argv=None):
    parser = argparse.ArgumentParser(description="Collect and validate Morocco traffic observations")
    parser.add_argument("--init-db", action="store_true", help="Create or migrate schema and seed monitoring points")
    parser.add_argument("--demo", action="store_true", help="Load seven days of explicitly labeled synthetic observations")
    parser.add_argument("--cities", nargs="+", choices=sorted(catalog().city.unique()))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    engine = make_engine(database_url())
    if args.init_db or args.demo:
        initialize_database(engine)
    if args.init_db and not args.demo:
        logger.info("Schema ready: 12 cities and 36 monitoring points")
        return 0
    started_at = datetime.now(timezone.utc)
    run_id = str(uuid.uuid4())
    if args.demo:
        frame = demo_data(started_at)
        if args.cities:
            frame = frame[frame.city.isin(args.cities)]
        records = frame.to_dict("records")
        count = save_batch(engine, records, run_id, started_at, "demo", len(records), 0)
        logger.info("Stored %s synthetic observations", count)
        return 0
    api_key = os.getenv("TOMTOM_API_KEY")
    if not api_key:
        logger.error("Set TOMTOM_API_KEY before collecting live data")
        return 2
    points = catalog()
    if args.cities:
        points = points[points.city.isin(args.cities)]
    records = []
    with api_session() as session:
        for point in points.to_dict("records"):
            flow = fetch_traffic(session, api_key, point["lat"], point["lon"])
            if flow is not None:
                records.append({**point, **flow, "recorded_at": datetime.now(timezone.utc)})
    failed = len(points) - len(records)
    count = save_batch(engine, records, run_id, started_at, "tomtom", len(points), failed)
    logger.info("Stored %s observations; %s of %s points failed", count, failed, len(points))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
