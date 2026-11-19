CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS cities (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    region TEXT NOT NULL
);
INSERT INTO cities (name, region) VALUES ('Casablanca', 'Casablanca-Settat')
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS locations (
    id SERIAL PRIMARY KEY,
    city_id INTEGER NOT NULL REFERENCES cities(id),
    name TEXT NOT NULL,
    lat DOUBLE PRECISION NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon DOUBLE PRECISION NOT NULL CHECK (lon BETWEEN -180 AND 180),
    geom GEOMETRY(Point, 4326),
    UNIQUE (city_id, name)
);
ALTER TABLE locations ADD COLUMN IF NOT EXISTS city_id INTEGER REFERENCES cities(id);
UPDATE locations SET city_id = (SELECT id FROM cities WHERE name = 'Casablanca') WHERE city_id IS NULL;
ALTER TABLE locations ALTER COLUMN city_id SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_locations_city_name ON locations (city_id, name);
CREATE INDEX IF NOT EXISTS idx_locations_geom ON locations USING GIST (geom);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id TEXT PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    source TEXT NOT NULL,
    requested INTEGER NOT NULL,
    succeeded INTEGER NOT NULL,
    failed INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS traffic_readings (
    id SERIAL PRIMARY KEY,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    current_speed DOUBLE PRECISION NOT NULL CHECK (current_speed >= 0),
    free_flow_speed DOUBLE PRECISION NOT NULL CHECK (free_flow_speed > 0),
    congestion_ratio DOUBLE PRECISION NOT NULL CHECK (congestion_ratio >= 0),
    confidence DOUBLE PRECISION CHECK (confidence BETWEEN 0 AND 1),
    road_closure BOOLEAN NOT NULL DEFAULT FALSE,
    source TEXT NOT NULL DEFAULT 'tomtom',
    run_id TEXT REFERENCES ingestion_runs(id),
    raw_payload JSONB,
    UNIQUE (location_id, recorded_at, source)
);
ALTER TABLE traffic_readings ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION;
ALTER TABLE traffic_readings ADD COLUMN IF NOT EXISTS road_closure BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE traffic_readings ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'tomtom';
ALTER TABLE traffic_readings ADD COLUMN IF NOT EXISTS run_id TEXT REFERENCES ingestion_runs(id);
ALTER TABLE traffic_readings ADD COLUMN IF NOT EXISTS raw_payload JSONB;
CREATE UNIQUE INDEX IF NOT EXISTS idx_readings_identity ON traffic_readings (location_id, recorded_at, source);
CREATE INDEX IF NOT EXISTS idx_readings_time ON traffic_readings (recorded_at);
CREATE INDEX IF NOT EXISTS idx_readings_location_time ON traffic_readings (location_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_readings_source_time ON traffic_readings (source, recorded_at DESC);

CREATE OR REPLACE VIEW city_hourly_traffic AS
SELECT c.name AS city, date_trunc('hour', r.recorded_at) AS hour, r.source,
       count(*) AS samples, count(DISTINCT l.id) AS locations,
       avg(r.current_speed) AS average_speed,
       avg(100 * (1 - LEAST(1, r.congestion_ratio))) AS congestion_pct,
       avg(r.confidence) AS average_confidence
FROM traffic_readings r
JOIN locations l ON l.id = r.location_id
JOIN cities c ON c.id = l.city_id
GROUP BY c.name, date_trunc('hour', r.recorded_at), r.source;

ALTER TABLE traffic_readings ALTER COLUMN location_id SET NOT NULL;
ALTER TABLE traffic_readings ALTER COLUMN current_speed SET NOT NULL;
ALTER TABLE traffic_readings ALTER COLUMN free_flow_speed SET NOT NULL;
ALTER TABLE traffic_readings ALTER COLUMN congestion_ratio SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'traffic_valid_speeds' AND conrelid = 'traffic_readings'::regclass) THEN
        ALTER TABLE traffic_readings ADD CONSTRAINT traffic_valid_speeds
        CHECK (current_speed >= 0 AND current_speed < 'Infinity'::float8
               AND free_flow_speed > 0 AND free_flow_speed < 'Infinity'::float8
               AND congestion_ratio >= 0 AND congestion_ratio < 'Infinity'::float8);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'traffic_valid_confidence' AND conrelid = 'traffic_readings'::regclass) THEN
        ALTER TABLE traffic_readings ADD CONSTRAINT traffic_valid_confidence
        CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'locations_valid_coordinates' AND conrelid = 'locations'::regclass) THEN
        ALTER TABLE locations ADD CONSTRAINT locations_valid_coordinates
        CHECK (lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180);
    END IF;
END $$;
