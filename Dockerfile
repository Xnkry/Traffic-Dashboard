FROM ghcr.io/astral-sh/uv:0.13.7 AS uv
FROM python:3.14-slim
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev
COPY dashboard.py traffic_data.py traffic_views.py city_catalog.py ingest.py collector.py road_analysis.py road_store.py road_ingest.py road_dashboard.py network_inventory.py motorway.py motorway_dashboard.py ./
COPY .streamlit/config.toml .streamlit/config.toml
COPY db ./db
COPY data/cities.csv data/cities.csv
RUN useradd --create-home atlas && chown -R atlas:atlas /app
USER atlas
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health')"
CMD ["streamlit", "run", "dashboard.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
