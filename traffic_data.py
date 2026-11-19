import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import (
    JSON, Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey,
    Index, Integer, MetaData, String, Table, UniqueConstraint, create_engine, event, select,
)

from city_catalog import catalog


metadata = MetaData()
cities = Table("cities", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String, nullable=False, unique=True),
    Column("region", String, nullable=False))
locations = Table("locations", metadata,
    Column("id", Integer, primary_key=True),
    Column("city_id", ForeignKey("cities.id"), nullable=False),
    Column("name", String, nullable=False),
    Column("lat", Float, nullable=False), Column("lon", Float, nullable=False),
    UniqueConstraint("city_id", "name"),
    CheckConstraint("lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180"))
batches = Table("ingestion_runs", metadata,
    Column("id", String, primary_key=True),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Column("source", String, nullable=False),
    Column("requested", Integer, nullable=False),
    Column("succeeded", Integer, nullable=False),
    Column("failed", Integer, nullable=False))
readings = Table("traffic_readings", metadata,
    Column("id", Integer, primary_key=True),
    Column("location_id", ForeignKey("locations.id"), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("current_speed", Float, nullable=False),
    Column("free_flow_speed", Float, nullable=False),
    Column("congestion_ratio", Float, nullable=False),
    Column("confidence", Float), Column("road_closure", Boolean, nullable=False, default=False),
    Column("source", String, nullable=False, default="tomtom"),
    Column("run_id", ForeignKey("ingestion_runs.id")), Column("raw_payload", JSON),
    UniqueConstraint("location_id", "recorded_at", "source"),
    CheckConstraint("current_speed >= 0 AND free_flow_speed > 0 AND congestion_ratio >= 0"),
    CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)"))
Index("idx_readings_location_time", readings.c.location_id, readings.c.recorded_at)
Index("idx_readings_source_time", readings.c.source, readings.c.recorded_at)


def database_url() -> str:
    return os.getenv("DATABASE_URL", "sqlite:///data/traffic.db")


def make_engine(url: str):
    args = {"connect_timeout": 5} if url.startswith("postgresql") else {}
    engine = create_engine(url, pool_pre_ping=True, connect_args=args)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
    return engine


def initialize_database(engine):
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.exec_driver_sql((Path(__file__).parent / "db/schema.sql").read_text())
    else:
        metadata.create_all(engine)
    seed_catalog(engine)


def insert_ignore(connection, table, values, keys):
    if connection.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = insert(table).values(values).on_conflict_do_nothing(index_elements=keys)
    return connection.execute(statement)


def seed_catalog(engine):
    with engine.begin() as connection:
        for city, group in catalog().groupby("city"):
            insert_ignore(connection, cities, {"name": city, "region": group.iloc[0].region}, ["name"])
            city_id = connection.scalar(select(cities.c.id).where(cities.c.name == city))
            for row in group.itertuples():
                insert_ignore(connection, locations,
                    {"city_id": city_id, "name": row.location, "lat": row.lat, "lon": row.lon},
                    ["city_id", "name"])
        if engine.dialect.name == "postgresql":
            connection.exec_driver_sql("UPDATE locations SET geom = ST_SetSRID(ST_MakePoint(lon, lat), 4326) WHERE geom IS NULL")


def load_readings(engine, source="tomtom", days=7, now=None):
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    statement = select(
        cities.c.name.label("city"), cities.c.region,
        locations.c.name.label("location"), locations.c.lat, locations.c.lon,
        readings.c.recorded_at, readings.c.current_speed, readings.c.free_flow_speed,
        readings.c.congestion_ratio, readings.c.confidence, readings.c.road_closure,
        readings.c.source,
    ).select_from(readings.join(locations).join(cities)).where(
        readings.c.source == source, readings.c.recorded_at >= cutoff
    ).order_by(readings.c.recorded_at)
    with engine.connect() as connection:
        frame = pd.read_sql(statement, connection)
    frame["location_key"] = frame.city + "/" + frame.location
    return enrich(frame)


def enrich(frame):
    frame = frame.copy()
    frame["recorded_at"] = pd.to_datetime(frame.recorded_at, utc=True)
    frame["congestion_pct"] = (1 - frame.congestion_ratio.clip(0, 1)) * 100
    frame["status"] = np.select(
        [frame.road_closure.fillna(False).astype(bool), frame.congestion_ratio < .4, frame.congestion_ratio < .7],
        ["Closed", "Heavy", "Moderate"], default="Flowing")
    return frame


def demo_data(now=None, days=7, selected_cities=None):
    end = pd.Timestamp(now or datetime.now(timezone.utc)).floor("15min")
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    times = pd.date_range(end=end, periods=days * 96, freq="15min")
    local = times.tz_convert("Africa/Casablanca")
    hours = local.hour.to_numpy() + local.minute.to_numpy() / 60
    rush = np.exp(-((hours - 8.5) / 1.7) ** 2) + np.exp(-((hours - 18) / 2.1) ** 2)
    rng = np.random.default_rng(42)
    frames = []
    points = catalog()
    if selected_cities is not None:
        points = points[points.city.isin(selected_cities)]
    for index, row in enumerate(points.to_dict("records")):
        free = [50, 60, 80][index % 3]
        ratio = np.clip(.9 - (.35 + index % 5 * .07) * rush
            + .04 * np.sin(hours + index) + rng.normal(0, .035, len(times)), .12, 1)
        frame = pd.DataFrame({**row, "recorded_at": times,
            "current_speed": np.round(free * ratio, 1), "free_flow_speed": free,
            "congestion_ratio": ratio, "confidence": rng.uniform(.82, .99, len(times)),
            "road_closure": False, "source": "demo"})
        frames.append(frame)
    return enrich(pd.concat(frames, ignore_index=True))


def snapshot(frame, at=None):
    if at is not None:
        frame = frame[frame.recorded_at <= at]
    return frame.sort_values("recorded_at").drop_duplicates("location_key", keep="last").copy()


def validate_flow(flow):
    current, free = float(flow["currentSpeed"]), float(flow["freeFlowSpeed"])
    confidence = flow.get("confidence")
    if not math.isfinite(current) or not math.isfinite(free) or current < 0 or free <= 0:
        raise ValueError("Invalid speed measurement")
    if confidence is not None:
        confidence = float(confidence)
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("Invalid confidence")
    closure = flow.get("roadClosure", False)
    if not isinstance(closure, bool):
        raise ValueError("Invalid closure flag")
    return {"current_speed": current, "free_flow_speed": free,
        "congestion_ratio": current / free, "confidence": confidence,
        "road_closure": closure}
