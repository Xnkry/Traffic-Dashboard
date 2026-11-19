# Deploy Atlas and keep traffic fresh

Use a Linux server with Docker Engine and the Docker Compose plugin. The production stack runs PostgreSQL/PostGIS, a one-time schema migration, Streamlit, and an independent collector. The collector continues working when nobody has the dashboard open.

## 1. Configure the server

```bash
git clone https://github.com/Xnkry/Traffic-Dashboard.git
cd Traffic-Dashboard
cp .env.production.example .env.production
openssl rand -hex 32
```

Edit `.env.production`. Paste the generated hex string into `POSTGRES_PASSWORD` and your key into `TOMTOM_API_KEY`. Use a hex password so it is also safe inside the connection URL. Keep this file on the server; it is ignored by Git and excluded from the image.

Default collection is **up to 36 points once per hour**. This is a conservative starting configuration, not a promise that it fits every provider plan. The catalog has 475 cities/towns and 499 monitoring points. Set `COLLECT_MAX_POINTS=0` to collect all selected points. `COLLECT_CITIES` optionally limits collection to comma-separated names exactly as shown in the dashboard; leave it blank for all places.

```dotenv
COLLECT_INTERVAL_SECONDS=3600
COLLECT_MAX_POINTS=0
COLLECT_CITIES=
```

A full 499-point batch makes 499 requests before retries. That is approximately **11,976 requests/day hourly**, **47,904/day every 15 minutes**, or **143,712/day every 5 minutes**. Check your TomTom account allowance before choosing the interval. Each failed request may have up to three retries. The point limit selects the first N catalog points (places are ordered by GeoNames population); it does not rotate coverage. Selecting cities in the dashboard does not change the worker's selection.

## 2. Start the stack

```bash
docker compose --env-file .env.production -f compose.production.yml up -d --build
docker compose --env-file .env.production -f compose.production.yml ps
docker compose --env-file .env.production -f compose.production.yml logs -f collector
```

Migration completes before the dashboard and collector start. The collector immediately requests a batch, then repeats at the configured interval. Partial failures are recorded in collection history and retried on the next batch. A PostgreSQL advisory lock prevents overlapping live batches against the same database, even if another collector or manual command is started.

The database is private to the Docker network. The dashboard listens on the server's loopback interface. For a private preview from your own computer:

```bash
ssh -L 8501:127.0.0.1:8501 user@your-server
```

Open `http://localhost:8501` while the SSH connection remains open.

## 3. Publish with HTTPS

Point a hostname's DNS A record to the server IP (and configure AAAA only if the server has working IPv6). Set `DOMAIN=traffic.yourdomain.com` in `.env.production`. Permit inbound TCP ports 80 and 443 on the server and hosting firewall.

```bash
docker compose --env-file .env.production -f compose.production.yml -f compose.https.yml up -d --build
```

Caddy obtains and renews the TLS certificate and proxies Streamlit, including its WebSocket connection. Visit `https://traffic.yourdomain.com`. This publishes a publicly readable traffic dashboard; add access controls at your reverse proxy if you want a private service. HTTPS cannot be verified until you configure a real hostname and deploy to a reachable server.

## Refresh behavior

There are two independent schedules:

1. **Collector:** TomTom API → validation → PostgreSQL. Runs in the background on the server, even with no browser open. Configure `COLLECT_INTERVAL_SECONDS` and restart the collector after changing it.
2. **Dashboard:** PostgreSQL → charts, map, and metrics. The sidebar's **Auto-refresh dashboard** toggle defaults on for live data. Choose 30, 60, 120, or 300 seconds. Streamlit's timer operates while the browser session is active; it does not make provider requests.

Dashboard filters remain selected across refreshes. Historical replay is available; the timer rereads data but does not manufacture new readings. The freshness warning appears for observations older than 30 minutes, so hourly collection may legitimately show a warning between batches.

## Run automation on your current computer

Your existing development `.env` uses the database already running locally:

```bash
uv run --env-file .env python ingest.py --init-db
uv run --env-file .env python collector.py --interval 3600 --max-points 36
```

Keep that process running, or use the production container for automatic restart after a reboot. For the entire catalog:

```bash
uv run --env-file .env python collector.py --interval 3600 --max-points 0
```

To test exactly one small batch and exit:

```bash
uv run --env-file .env python collector.py --once --cities Casablanca --max-points 3
```

The worker handles SIGTERM/SIGINT, terminates its current subprocess on shutdown, and bounds each batch to 600 seconds by default (`--timeout` overrides this). Only run one worker per database; do not scale collector replicas. Database ingestion is atomic, so an interrupted batch does not leave partially inserted readings.

## Backups, updates, and logs

Production uses its own named PostgreSQL volume. It does not automatically contain the readings from your development computer. Back up development data with `pg_dump` and restore it to a fresh production database before starting the migration if you want to carry history over. Keep the original backup until the restored data has been verified.

```bash
docker compose --env-file .env.production -f compose.production.yml exec -T db pg_dump -U traffic_user traffic_db > traffic-backup.sql
```

Schedule backups with your server's backup system and store a copy off the server. Do not use `docker compose down -v` unless you intend to delete all stored readings. Changing `POSTGRES_PASSWORD` in the environment does not change the password inside an existing database volume; perform a proper database password rotation instead.

For an update, back up first, then run:

```bash
git pull
docker compose --env-file .env.production -f compose.production.yml stop collector dashboard
docker compose --env-file .env.production -f compose.production.yml build migrate
docker compose --env-file .env.production -f compose.production.yml run --rm migrate
docker compose --env-file .env.production -f compose.production.yml up -d --force-recreate dashboard collector
```

Include `-f compose.https.yml` when operating the HTTPS stack. The application reads the latest seven days, but historical rows remain in PostgreSQL; monitor storage and define a retention policy appropriate to your needs. The `Pipeline health` view shows collection successes and failures, and `logs collector` shows service failures. A healthy web server alone does not mean the provider is returning data.

## Catalog provenance and coverage

The bundled CSV contains 475 GeoNames entries classified as populated places or administrative seats with country code `MA`; neighborhood subdivisions (`PPLX`) are excluded. It is the complete selected subset of the downloaded `cities500` source, not a claim to enumerate every municipality or village. Country labels follow the source data. Existing district coordinates are preserved; new places use one GeoNames center point. API road coverage is checked only when a point is collected and may be unavailable at some coordinates.

GeoNames is licensed under CC BY 4.0. Attribution: [GeoNames](https://www.geonames.org/). The source URL, retrieval timestamp, SHA-256, and filters are stored in `data/cities-source.json`. Population is source metadata and may reflect different census years; it is used only for ordering.

Refresh the source explicitly, review the CSV changes, then reseed:

```bash
uv run python scripts/update_city_catalog.py
uv run --env-file .env python ingest.py --init-db
```

Catalog seeding only adds places and points; it does not delete historical observations or silently rename database records. Review upstream name changes before replacing a production catalog.

References: [Streamlit Docker deployment](https://docs.streamlit.io/deploy/tutorials/docker), [Streamlit timed fragments](https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment), [GeoNames data](https://download.geonames.org/export/dump/), [TomTom market coverage](https://docs.tomtom.com/traffic-api/documentation/tomtom-maps/v1/product-information/market-coverage).
