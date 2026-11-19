# Casablanca–Marrakech motorway study

Open **Motorway study** in the dashboard and select **Live database**. This workspace is independent of the city sidebar filters.

The initial inventory selects mapped A3 main carriageways within longitude −8.2 to −7.2 and latitude 31.68 to 33.58. These are explicit study limits near Casablanca and Marrakech, not official kilometre posts or a guaranteed connected navigation route. Ramps, A31 approaches and urban streets are excluded. Both mapped carriageways are retained; traffic-provider direction matching is not guaranteed. ADM identifies Casablanca–Agadir via Marrakech as A3: https://www.adm.co.ma/fr/nos-autoroutes/a3-casablanca-agadir

## Import and collect

```bash
uv run --env-file .env python road_ingest.py import --city "Casablanca–Marrakech motorway"
uv run --env-file .env python road_ingest.py collect --city "Casablanca–Marrakech motorway" --max-segments 30
uv run --env-file .env python collector.py --road-city "Casablanca–Marrakech motorway" --max-points 50 --interval 300
```

If Overpass is unavailable, add `--network-file /path/to/overpass.json` to the import command. The file must contain Overpass way geometry and A3 highway tags spanning both study endpoints. Empty, malformed and obviously partial inventories are rejected before replacing the database inventory. Internal mapping gaps may remain.

For Docker Compose deployment, set `COLLECT_ROAD_CITY=Casablanca–Marrakech motorway`, `COLLECT_MAX_POINTS=50` and `COLLECT_INTERVAL_SECONDS=300` in the production environment. Follow README deployment instructions; run the import against the same persistent database before starting the collector. The dashboard reads stored evidence; refreshing its page does not call the traffic API.

## Database and collection

The existing road tables store this corridor under its distinct study name: versioned geometry in `road_segments`, timestamped speed and confidence in `road_observations`, inventory provenance in `road_import_scopes`, and incident check snapshots in `road_incident_polls`. City inventories are preserved. Imports and collection use the existing PostgreSQL advisory lock.

Road geometry is split into samples up to 400 m. The worker rotates oldest-attempted samples, including failed requests. A batch uses up to the selected number of flow requests plus one incident request per occupied 0.25-degree corridor tile (and possible retries). Incident IDs are deduplicated and spatially filtered to approximately 60 m from the inventory. Any failed tile makes the batch incident status incomplete rather than reporting zero accidents. Partial reports remain in the stored snapshot for audit.

A complete sweep takes approximately `ceil(inventory_samples / batch_size) × interval`, plus collection overrun. A 50-sample batch every five minutes will not keep thousands of samples fresh within the 30-minute freshness window. Choose a budget supported by the API plan, narrow collection using an imported corridor ID, or arrange a network-wide licensed traffic feed. Collection is sequential and subject to the worker timeout; successful observations are retained on interruption.

## Analysis and interpretation

The interactive map shows reliable current provider geometry in teal/amber/red, inventory without current evidence in gray, and reported incidents at their provider point or line midpoint. Hover reveals speeds and report details. Latitude selection narrows the study; incident filtering can show accidents only. Line midpoints summarize affected stretches and are not exact crash coordinates.

Speed loss is `100 × (1 − current_speed / free_flow_speed)`, clipped at zero. More than 30% loss is moderate; more than 60% is heavy. Closures override this classification. Confidence below 0.5, failed observations and observations older than 30 minutes are not presented as current reliable flow.

The historical heatmap uses median loss per 0.05-degree latitude band and 12-hour bin; the selected sampling-point chart compares hourly median speed with free-flow speed. Bands combine both carriageways; repeated provider geometries can occur at different samples. Blank cells mean missing observations. These are sampled speed statistics, not full-network vehicle-weighted averages. Free-flow speed is not a legal speed limit.

Weekly baselines need at least four prior distinct dates for matching sample, provider geometry, weekday and hour. Nearby incident reports are evidence, not proof of the cause of congestion. Speed data alone cannot measure traffic volume, occupancy, or prove excessive demand. Zero reported accidents does not prove no accident occurred.

Road geometry: OpenStreetMap contributors, ODbL, https://www.openstreetmap.org/copyright. Incident API: https://docs.tomtom.com/traffic-api/documentation/tomtom-maps/v1/traffic-incidents/incident-details

## Import troubleshooting

Imports log each map-server request and response. By default they try two servers, with a five-second connect timeout and 45-second read inactivity timeout per request. `OVERPASS_URLS` accepts a comma-separated HTTPS endpoint list; `OVERPASS_READ_TIMEOUT` accepts 10–180 seconds. A server returning an incomplete response is never used. These are per-request inactivity timeouts, not a total import deadline.

If both servers fail, retry later or supply an Overpass JSON file with `--network-file`. Ctrl-C cancels with a short message. The collector exits with status 2 when the study inventory is missing, rather than retrying that setup error indefinitely. Run the import successfully before starting the collector, using the same `DATABASE_URL` / `.env` for both.
