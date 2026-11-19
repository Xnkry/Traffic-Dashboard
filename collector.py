import argparse
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event
import time

from city_catalog import catalog
from motorway import STUDY


def collection_command(cities, max_points, road_city=None, avenue=None, corridor=None):
    if road_city:
        command = [sys.executable, str(Path(__file__).with_name("road_ingest.py")), "collect", "--city", road_city, "--max-segments", str(max_points or 50)]
        return command + (["--avenue", avenue] if avenue else []) + (["--corridor", corridor] if corridor else [])
    command = [sys.executable, str(Path(__file__).with_name("ingest.py")), "--max-points", str(max_points)]
    if cities:
        command.extend(["--cities", *cities])
    return command


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one sequential traffic collector on a fixed interval")
    parser.add_argument("--interval", type=int, default=int(os.getenv("COLLECT_INTERVAL_SECONDS", "3600")))
    parser.add_argument("--max-points", type=int, default=int(os.getenv("COLLECT_MAX_POINTS", "0")))
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--corridor", default=os.getenv("COLLECT_CORRIDOR"), help="Limit road mode to a connected corridor ID")
    parser.add_argument("--avenue", default=os.getenv("COLLECT_AVENUE"), help="Limit road mode to an exact avenue name")
    parser.add_argument("--road-city", default=os.getenv("COLLECT_ROAD_CITY"), help="Collect avenue flow and incidents for one imported city")
    parser.add_argument("--cities", nargs="+", default=[x.strip() for x in os.getenv("COLLECT_CITIES", "").split(",") if x.strip()])
    args = parser.parse_args(argv)
    if args.interval < 60 or args.max_points < 0 or args.timeout < 1:
        parser.error("Use interval >= 60 seconds, max-points >= 0, and timeout >= 1 second")
    if (args.avenue or args.corridor) and not args.road_city:
        parser.error("--avenue requires --road-city")
    if args.road_city and args.road_city not in set(catalog().city) | {STUDY}:
        parser.error("Unknown road city")
    if not set(args.cities).issubset(set(catalog().city)):
        parser.error("COLLECT_CITIES contains an unknown city")
    if not os.getenv("TOMTOM_API_KEY"):
        parser.error("Set TOMTOM_API_KEY before starting the collector")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    points = catalog()
    if args.cities:
        points = points[points.city.isin(args.cities)]
    count = min(len(points), args.max_points) if args.max_points else len(points)
    logging.info("Collecting %s points every %ss; approximately %.0f requests/day before retries", count, args.interval, count * 86400 / args.interval)
    if args.road_city:
        logging.info("Avenue mode: %s, at most %s road samples plus incident requests per batch (motorway mode uses corridor tiles)", args.road_city, args.max_points or 50)
    command = collection_command(args.cities, args.max_points, args.road_city, args.avenue, args.corridor)
    while not stop.is_set():
        started = time.monotonic()
        result = 1
        with subprocess.Popen(command) as child:
            while child.poll() is None:
                if stop.wait(1) or time.monotonic() - started >= args.timeout:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    logging.warning("Collection interrupted or timed out; next interval will retry")
                    break
            else:
                result = child.returncode
        if args.once or (args.road_city and result == 2):
            if args.road_city and result == 2:
                logging.error("Collection stopped: import the road inventory successfully before starting this worker.")
            return result
        if result:
            logging.warning("Collection finished with status %s; retrying at the next interval", result)
        stop.wait(max(1, args.interval - (time.monotonic() - started)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
