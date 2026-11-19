from datetime import datetime, timedelta, timezone
import hashlib

import pandas as pd
from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, MetaData, String, Table, select

from traffic_data import insert_ignore


metadata = MetaData()
areas = Table("road_areas", metadata,
    Column("city", String, primary_key=True), Column("bbox", JSON, nullable=False),
    Column("radius_km", Float, nullable=False), Column("imported_at", DateTime(timezone=True), nullable=False))
imports = Table("road_import_scopes", metadata,
    Column("city", ForeignKey("road_areas.city"), primary_key=True),
    Column("scope", String, nullable=False), Column("boundary_id", String), Column("boundary_name", String))
segments = Table("road_segments", metadata,
    Column("id", String, primary_key=True), Column("city", ForeignKey("road_areas.city"), nullable=False),
    Column("name", String, nullable=False), Column("osm_id", String, nullable=False),
    Column("road_class", String, nullable=False), Column("oneway", String),
    Column("geometry", JSON, nullable=False), Column("lat", Float, nullable=False), Column("lon", Float, nullable=False),
    Column("length_m", Float, nullable=False), Column("active", Boolean, nullable=False),
    Column("last_attempt", DateTime(timezone=True)))
observations = Table("road_observations", metadata,
    Column("id", Integer, primary_key=True), Column("segment_id", ForeignKey("road_segments.id"), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False), Column("status", String, nullable=False),
    Column("current_speed", Float), Column("free_flow_speed", Float), Column("speed_loss", Float),
    Column("confidence", Float), Column("road_closure", Boolean), Column("geometry", JSON),
    Column("provider_key", String), Column("match_distance_m", Float), Column("raw_payload", JSON))
polls = Table("road_incident_polls", metadata,
    Column("id", Integer, primary_key=True), Column("city", ForeignKey("road_areas.city"), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False), Column("success", Boolean, nullable=False),
    Column("bbox", JSON, nullable=False), Column("payload", JSON, nullable=False))
Index("idx_road_city_active", segments.c.city, segments.c.active)
Index("idx_road_segment_time", observations.c.segment_id, observations.c.observed_at)
Index("idx_road_poll_time", polls.c.city, polls.c.observed_at)


def initialize(engine):
    metadata.create_all(engine)


def save_inventory(engine, city, bbox, radius_km, records, scope="bbox", boundary_id=None, boundary_name=None):
    if not records:
        raise ValueError("Empty road inventory; previous data preserved")
    initialize(engine)
    with engine.begin() as conn:
        insert_ignore(conn, areas, {"city": city, "bbox": bbox, "radius_km": radius_km,
                      "imported_at": datetime.now(timezone.utc)}, ["city"])
        conn.execute(areas.update().where(areas.c.city == city).values(bbox=bbox, radius_km=radius_km, imported_at=datetime.now(timezone.utc)))
        details = {"city": city, "scope": scope, "boundary_id": boundary_id, "boundary_name": boundary_name}
        insert_ignore(conn, imports, details, ["city"])
        conn.execute(imports.update().where(imports.c.city == city).values(**details))
        conn.execute(segments.update().where(segments.c.city == city).values(active=False))
        for record in records:
            insert_ignore(conn, segments, record, ["id"])
            conn.execute(segments.update().where(segments.c.id == record["id"]).values(active=True))



def label_corridors(roads):
    roads = roads.copy()
    if roads.empty:
        roads["corridor_id"] = pd.Series(dtype=str)
        roads["corridor_label"] = pd.Series(dtype=str)
        return roads
    records = roads.to_dict("records")
    parent = list(range(len(records)))
    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    nodes = {}
    for i, row in enumerate(records):
        for point in row["geometry"]:
            key = (row["name"], round(point[0], 6), round(point[1], 6))
            if key in nodes:
                parent[root(i)] = root(nodes[key])
            else:
                nodes[key] = i
    groups = {}
    for i, row in enumerate(records):
        groups.setdefault((row["name"], root(i)), []).append(i)
    counts = {}
    for name, _ in groups:
        counts[name] = counts.get(name, 0) + 1
    ordinal = {}
    for (name, _), indices in sorted(groups.items(), key=lambda item: min(records[i]["id"] for i in item[1])):
        ordinal[name] = ordinal.get(name, 0) + 1
        key = hashlib.sha256(min(records[i]["id"] for i in indices).encode()).hexdigest()[:16]
        label = name if counts[name] == 1 else f"{name} · sector {ordinal[name]}"
        for i in indices:
            records[i]["corridor_id"], records[i]["corridor_label"] = key, label
    return pd.DataFrame(records)


def load_city(engine, city, days=56):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with engine.connect() as conn:
        roads = pd.read_sql(select(segments).where(segments.c.city == city, segments.c.active.is_(True)), conn)
        history = pd.read_sql(select(observations).join(segments).where(
            segments.c.city == city, segments.c.active.is_(True), observations.c.observed_at >= cutoff), conn)
        poll = conn.execute(select(polls).where(polls.c.city == city).order_by(polls.c.observed_at.desc(), polls.c.id.desc()).limit(1)).mappings().first()
        area = conn.execute(select(areas).where(areas.c.city == city)).mappings().first()
        details = conn.execute(select(imports).where(imports.c.city == city)).mappings().first()
    return label_corridors(roads), history, dict(poll) if poll else None, {**dict(area), **dict(details or {})} if area else None


def load_incident_history(engine, city, days=56):
    with engine.connect() as conn:
        result = pd.read_sql(select(polls).where(polls.c.city == city,
            polls.c.observed_at >= datetime.now(timezone.utc) - timedelta(days=days)).order_by(polls.c.observed_at), conn)
    return result
