# Avenue-level traffic intelligence

The objective is to map congestion on individual roads, show reported accidents and other disruptions, and compare each observed segment with its usual pattern. City-center sample points cannot answer this question; the Avenue intelligence workspace uses a separate road inventory, observation history, and incident feed.

## What is implemented

- Extract mapped drivable roads from a city/commune administrative area in OpenStreetMap, including named avenues, connecting roads, residential streets, and eligible unnamed roads. Footpaths, cycleways, private/no-access roads, and road areas are excluded. The importer resolves matching administrative boundaries or accepts an explicit Overpass area ID.
- Split long OSM ways into sample paths of at most 400 m and query their midpoints. Keep OSM way IDs, road names, class, one-way tags, geometry versions, and import scope. Roads that share a name are grouped only when their geometries connect at a mapped node. Disconnected namesakes are shown as separate sectors; individual samples remain distinguishable. Missing links in OSM can split a real avenue, so verify important corridors visually.
- Collect speed, free-flow speed, confidence, closure status, and actual provider geometry. Reject association when the nearest returned geometry is more than 75 m from the requested point. The flow service may return one direction or a nearby parallel road; this does not guarantee coverage of every lane or carriageway.
- Query present traffic incidents independently. Distinguish reported accidents, congestion, roadworks, closures, weather, and breakdowns. Preserve successful empty responses separately from failed requests.
- Color fresh provider paths green, amber, or red; leave inventory without reliable current readings gray. Display freshness and sample coverage by road.
- Compare speed loss against the median of daily mean readings for the same sample, provider segment, weekday, and hour over the previous 56 days. Require four distinct previous dates with adequate confidence. Current-day and future data cannot enter the baseline.
- Preserve historical incident polls, show daily distinct reported accident IDs and failed polling counts, and provide downloadable avenue evidence.

“Full” means congested here: speed relative to free flow. The application does not have measured vehicle counts, density, road capacity, or occupancy. It cannot prove that a road is physically full or that excess demand caused a queue.

## Import a city network

```bash
uv run --env-file .env python road_ingest.py import --city Casablanca
```

The default resolves an administrative area matching the city name and requests all eligible OSM ways in that area. Ambiguous or missing matches fail explicitly. The selected area ID and name are displayed in the dashboard. Inspect them before making completeness claims; a commune can differ from the whole metropolitan area. Ways crossing the boundary may extend beyond it.

For a verified area:

```bash
uv run --env-file .env python road_ingest.py import --city Casablanca --area-id 3604072985
```

The identifier above was verified as the mapped Casablanca administrative area during development. Overpass area IDs derived from relations are generally relation ID + 3,600,000,000; inspect the intended relation before using a different ID. The importer works with any city in the bundled catalog, provided a suitable mapped area exists. It does not silently substitute an arbitrary bounding box for a failed boundary lookup.

For an explicitly bounded pilot:

```bash
uv run --env-file .env python road_ingest.py import --city Casablanca --radius-km 3
```

This is a rectangle extending approximately 3 km in each direction from the city catalog center, not all of Casablanca. It replaces the active inventory for that study city, while preserving previous observation rows. Reimport the administrative area to return to citywide inventory.

For nationwide or repeated bulk extraction, avoid hundreds of large public Overpass requests. Prepare per-city road GeoJSON from a regional OSM extract and verified administrative polygons using GIS tooling, then import locally:

```bash
uv run --env-file .env python road_ingest.py import --city Casablanca --network-file casablanca-roads.geojson
```

Accepted input: Overpass JSON containing `elements`, or GeoJSON FeatureCollection with LineString features and OSM-style `highway`, `name`, and `osm_id` properties. The file must already be filtered and clipped to the intended network. Its completeness and provenance remain the operator's responsibility. Coordinate order is longitude, latitude. Partial Overpass responses and empty inventories are rejected, preserving the previous inventory.

OSM-derived inventory is © OpenStreetMap contributors, under ODbL. Preserve this attribution and the applicable database license when distributing road geometry. [OSM copyright](https://www.openstreetmap.org/copyright).

## Resume extraction across the city catalog

```bash
uv run --env-file .env python network_inventory.py --all-cities --max-cities 3 --delay 60
```

This attempts the next three pending cities and writes an audit report to `data/network-import-report.csv`. It skips successful administrative/file imports, prioritizes unattempted cities over earlier failures, and refuses to claim success if the inventory was not actually stored. Re-run on a controlled schedule to progress through the catalog. `--cities Rabat Marrakech` limits the queue. Boundary ambiguities are recorded as failures for manual resolution; they are not silently converted into partial coverage. Only Casablanca has been imported and live-tested during this implementation, not the entire country.

For nationwide work, the preferred input is a prepared regional extract split by verified commune boundaries:

```bash
uv run --env-file .env python network_inventory.py --all-cities --max-cities 20 --network-dir data/city-networks --delay 10
```

Name each GeoJSON file with its GeoNames ID from `data/cities.csv`, for example `2553604.geojson` for Casablanca. No Overpass request is made in file mode. Missing files and failures are recorded. This supports complete, repeatable local extraction without repeatedly loading a public query service; generation and polygon verification of those input files remain a separate GIS preparation step.

## Collect flow and accidents

```bash
uv run --env-file .env python road_ingest.py collect --city Casablanca --max-segments 50
```

This makes one incident request for the inventory extent and up to 50 flow requests before retries. It does not attempt to poll thousands of roads accidentally. Samples rotate by oldest attempt, including failed samples, so a permanently unsupported road does not monopolize retries. PostgreSQL advisory locking prevents overlapping road collection/import jobs. Failed and mismatched readings remain explicit records.

Automate with the existing worker:

```bash
uv run --env-file .env python collector.py --road-city Casablanca --interval 300 --max-points 50
```

For one batch:

```bash
uv run --env-file .env python collector.py --road-city Casablanca --once --max-points 10
```

In production, import before enabling road mode:

```bash
docker compose --env-file .env.production -f compose.production.yml run --rm migrate python road_ingest.py import --city Casablanca
```

Then set these values in `.env.production` and recreate the collector:

```dotenv
COLLECT_ROAD_CITY=Casablanca
COLLECT_INTERVAL_SECONDS=300
COLLECT_MAX_POINTS=50
```

```bash
docker compose --env-file .env.production -f compose.production.yml up -d --build collector dashboard
```

Road mode replaces city-center collection for that worker. Leave `COLLECT_ROAD_CITY` empty for the original collector. A worker targets one imported city. For multiple cities, use a deliberate non-overlapping schedule with a shared request budget; do not launch hundreds of workers. Road observation attempts commit separately, so completed attempts survive a later timeout; each observation and its rotation timestamp are atomic. This differs from the original city-center collector's whole-batch transaction.

## Focus frequent collection on a connected avenue

Select a road in the dashboard and copy its corridor ID. Then target that corridor:

```bash
uv run --env-file .env python collector.py --road-city Casablanca --corridor CORRIDOR_ID --interval 300 --max-points 50
```

For a road-name filter across all its sectors, use `--avenue "Exact imported road name"` instead. `COLLECT_CORRIDOR` and `COLLECT_AVENUE` provide the corresponding production settings. A corridor ID is derived from its inventory version and can change after a network reimport; refresh the selection afterward. The full inventory remains visible even when collection targets a small research corridor.

## Interpreting the evidence

- **Nearby report: Accident:** the provider reports an accident within approximately 60 m of the observed flow path. This is an association, not proof that the accident caused the slowdown. Nearby parallel roads, overpasses, junctions, and direction differences can mislead proximity matching.
- **Consistent with recurring congestion:** current speed loss is close to a sufficiently supported, congested baseline and no nearby incident is reported in a fresh, time-aligned feed. This suggests a recurring pattern, not proven excessive vehicle demand.
- **Unusual slowdown; cause unconfirmed:** current speed loss exceeds the baseline by at least 20 percentage points. An unreported accident, roadworks, weather, signals, or another cause may explain it.
- **Insufficient history:** fewer than four comparable prior dates exist. No usual-pattern claim is made.
- **Unknown / stale / unavailable:** missing or unreliable evidence. Never interpreted as free-flow or no accidents.

Flow readings and the most recent incident poll must both be no older than 30 minutes and within 15 minutes of each other for current association. An empty successful feed means no incidents reported by this provider at that check. Daily incident counts are distinct provider IDs observed, not independently verified accident occurrence counts. Polling misses short events, and one continuing event can appear on multiple dates.

## Optimal collection strategy

First complete the road inventory for the exact study boundary. Then measure the gap between imported samples and provider-covered samples. Confirm named avenue associations and both directions on a representative sample before interpreting citywide results.

Choose sampling frequency from the budget, not from the number of cities advertised. For N samples, batch size B, and interval T minutes, a full rotation takes roughly `ceil(N/B) × T`. With 5,000 samples, 50 per batch, and a five-minute interval, the sweep takes 500 minutes—far too slow to call the entire network live. The dashboard shows this calculation. Freshness requires a larger request allowance, a smaller study network, or a licensed network-wide/bulk feed; map tiles alone are not a substitute for a historical analytical dataset.

Build at least four comparable weekdays of history, retaining incident snapshots and confidence. Examine avenues individually, compare matched non-incident periods, and report sample coverage beside averages. For a causal study, add vehicle counts, weather, roadworks, signal timings, and directional network matching; the current speed-and-incident evidence cannot isolate cause.

Begin with one verified city network, establish coverage and data quality, then scale the same process to other cities. Administrative extraction is not a guarantee of every real-world avenue: unmapped roads, missing names, boundaries, provider gaps, and changing road layouts remain measurable limitations.

References: [Overpass area/geometry queries](https://dev.overpass-api.de/overpass-doc/en/full_data/polygon.html), [TomTom flow segment API](https://docs.tomtom.com/traffic-api/documentation/tomtom-maps/v1/traffic-flow/flow-segment-data), [TomTom incident details](https://docs.tomtom.com/traffic-api/documentation/tomtom-maps/v1/traffic-incidents/incident-details).
