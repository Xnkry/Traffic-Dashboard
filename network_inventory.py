import argparse
import csv
from datetime import datetime, timezone
import logging
from pathlib import Path
import subprocess
import sys
import time

from sqlalchemy import select

from city_catalog import catalog
from road_store import imports, initialize
from traffic_data import database_url, make_engine


def pending_cities(reference, completed, previous):
    return sorted((city for city in reference if city not in completed), key=lambda city: previous.get(city, ""))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Resume auditable city/commune road extraction in bounded batches")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all-cities", action="store_true")
    selection.add_argument("--cities", nargs="+")
    parser.add_argument("--max-cities", type=int, default=3)
    parser.add_argument("--delay", type=int, default=60)
    parser.add_argument("--network-dir", type=Path, help="Prefer offline city GeoJSON files named by GeoNames ID")
    parser.add_argument("--report", type=Path, default=Path("data/network-import-report.csv"))
    args = parser.parse_args(argv)
    if not 1 <= args.max_cities <= 475 or args.delay < 10:
        parser.error("Use 1–475 cities per batch and delay >= 10 seconds")
    points = catalog().drop_duplicates("city")
    names = points.city.tolist() if args.all_cities else args.cities
    if not set(names).issubset(set(points.city)):
        parser.error("Unknown city")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    previous = {}
    if args.report.exists():
        with args.report.open(newline="") as source:
            for row in csv.DictReader(source):
                previous[row["city"]] = row["attempted_at"]
    failed = False
    engine = make_engine(database_url())
    try:
        initialize(engine)
        with engine.connect() as conn:
            completed = set(conn.scalars(select(imports.c.city).where(imports.c.scope.in_(["administrative", "file"]))))
        pending = pending_cities(names, completed, previous)[:args.max_cities]
        logging.info("%s requested cities already imported; attempting %s pending cities", len(set(names) & completed), len(pending))
        args.report.parent.mkdir(parents=True, exist_ok=True)
        for index, city in enumerate(pending):
            command = [sys.executable, str(Path(__file__).with_name("road_ingest.py")), "import", "--city", city]
            status, code = "failed", 1
            path = None
            if args.network_dir:
                geoname_id = points.loc[points.city == city, "geoname_id"].iloc[0]
                path = args.network_dir / f"{geoname_id}.geojson"
                command.extend(["--network-file", str(path)])
            if path is not None and not path.exists():
                status = "missing_network_file"
            else:
                try:
                    code = subprocess.run(command, timeout=600, check=False).returncode
                    with engine.connect() as conn:
                        scope = conn.scalar(select(imports.c.scope).where(imports.c.city == city))
                    status = "imported" if code == 0 and scope in {"administrative", "file"} else "failed_or_skipped"
                except subprocess.TimeoutExpired:
                    status, code = "timeout", 124
            new_file = not args.report.exists() or args.report.stat().st_size == 0
            with args.report.open("a", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=["attempted_at", "city", "status", "exit_code"])
                if new_file:
                    writer.writeheader()
                writer.writerow({"attempted_at": datetime.now(timezone.utc).isoformat(), "city": city, "status": status, "exit_code": code})
            failed = failed or status != "imported"
            logging.info("%s: %s", city, status)
            if index + 1 < len(pending):
                time.sleep(args.delay)
    finally:
        engine.dispose()
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
