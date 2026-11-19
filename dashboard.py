import os
import time
from datetime import datetime, timezone

import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from city_catalog import CITY_POINTS, catalog
from traffic_data import batches, database_url, demo_data, load_readings, make_engine, snapshot
from traffic_views import traffic_map, trend_chart, city_chart, observation_table


st.set_page_config(page_title="Atlas • Morocco traffic", page_icon=":material/route:", layout="wide")


@st.cache_resource(ttl=3600, max_entries=4)
def connection(url):
    return make_engine(url)


@st.cache_data(ttl=60, max_entries=6)
def read_database(url, source):
    return load_readings(connection(url), source=source)


@st.cache_data(ttl=900, max_entries=6)
def read_demo(city_names):
    return demo_data(selected_cities=city_names)


def configured_url():
    if os.getenv("DATABASE_URL"):
        return database_url()
    try:
        return st.secrets.get("DATABASE_URL", database_url())
    except FileNotFoundError:
        return database_url()


with st.sidebar:
    st.title(":material/route: Atlas")
    st.caption("MOROCCO / MOBILITY OBSERVATORY")
    page = st.radio("Workspace", ["Overview", "Avenue intelligence", "Patterns", "Data studio"], label_visibility="collapsed")
    source_options = ["Demo playground", "Live database", "Stored demo"]
    default_source = os.getenv("DEFAULT_DATA_SOURCE", "Demo playground")
    source = st.selectbox("Data source", source_options, index=source_options.index(default_source) if default_source in source_options else 0)
    st.caption("Synthetic observations" if source != "Live database" else "TomTom observations · automatic database refresh")
    st.space("small")
    reference = catalog()
    region = st.selectbox("Region", ["All regions", *sorted(reference.region.unique())])
    options = reference if region == "All regions" else reference[reference.region == region]
    all_cities = st.toggle("Select all cities and towns", value=False)
    defaults = sorted(options.city.unique()) if all_cities or region != "All regions" else sorted(city for city, _, _ in CITY_POINTS)
    selected_cities = st.multiselect("Cities", sorted(options.city.unique()), default=defaults)
    window = st.select_slider("History window", options=[6, 12, 24, 48, 72, 168], value=24, format_func=lambda x: f"{x} hours")
    if st.button("Refresh observations", icon=":material/refresh:", width="stretch"):
        read_database.clear()
        read_demo.clear()
    st.space("medium")
    auto_refresh = st.toggle("Auto-refresh dashboard", value=source == "Live database")
    refresh_seconds = st.selectbox("Refresh every", [30, 60, 120, 300], index=1, format_func=lambda n: f"{n} seconds", disabled=not auto_refresh)
    st.caption(f"{reference.city.nunique()} cities and towns · {len(reference)} monitoring points")
    st.caption("City center coordinates: GeoNames · CC BY 4.0")
    st.caption("Times displayed in Africa/Casablanca. Monitoring points sample nearby roads; they do not represent entire cities.")

st.session_state["last_page_refresh"] = time.monotonic()
st.session_state["last_page_refresh_utc"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


@st.fragment(run_every=refresh_seconds if auto_refresh else None)
def refresh_clock():
    elapsed = time.monotonic() - st.session_state["last_page_refresh"]
    if auto_refresh and elapsed >= refresh_seconds - 1:
        read_database.clear()
        st.rerun()
    if auto_refresh:
        st.caption(f"Dashboard refreshes every {refresh_seconds}s while open. Last refresh: {st.session_state['last_page_refresh_utc']}. API collection runs separately.")


refresh_clock()
if page == "Avenue intelligence":
    from road_dashboard import render
    render(configured_url(), selected_cities, source)
    st.stop()

st.caption("ATLAS / TRAFFIC INTELLIGENCE")
st.title({"Overview": "A clearer view of every city.", "Patterns": "Find the rhythm of the road.", "Data studio": "Good decisions start with good data."}[page])
st.markdown({"Overview": "Explore Morocco’s road network, one observation at a time.", "Patterns": "Compare cities, spot rush hours, and explore how traffic changes.", "Data studio": "Explore your observations, monitoring coverage, and collection history."}[page])

if not selected_cities:
    st.info("Choose at least one city in the sidebar to explore traffic.", icon=":material/location_city:")
    st.stop()

if source == "Demo playground":
    frame = read_demo(tuple(sorted(selected_cities)))
else:
    try:
        frame = read_database(configured_url(), "tomtom" if source == "Live database" else "demo")
    except (SQLAlchemyError, OSError, ValueError):
        st.error("The traffic database is unavailable or needs initialization.")
        st.code("uv run python ingest.py --init-db", language="bash")
        st.caption("Check DATABASE_URL in your environment or Streamlit secrets, then refresh. The demo playground is available without a database.")
        st.stop()

if frame.empty:
    st.info("No observations in the last seven days. Run the collector, then refresh.")
    st.code("uv run python ingest.py" if source == "Live database" else "uv run python ingest.py --demo", language="bash")
    st.stop()

now = pd.Timestamp(datetime.now(timezone.utc))
end = frame.recorded_at.max()
start = end - pd.Timedelta(hours=window)
filtered = frame[frame.city.isin(selected_cities) & (frame.recorded_at >= start)].copy()
if filtered.empty:
    st.info("No observations match these cities and this history window. Try a wider selection.")
    st.stop()

latest = snapshot(filtered)
local_end = end.tz_convert("Africa/Casablanca")
with st.container(horizontal=True):
    st.badge("SYNTHETIC DATA" if source != "Live database" else "DATABASE OBSERVATIONS", color="orange" if source != "Live database" else "blue")
    st.caption(f"Latest dataset observation {local_end:%d %b %Y · %H:%M} · {len(filtered):,} readings in selection")
if source != "Live database":
    st.caption("Demo values are generated for exploration. They are not measured traffic conditions.")
else:
    stale = int(((now - latest.recorded_at).dt.total_seconds() > 1800).sum())
    if stale:
        st.warning(f"{stale} of {len(latest)} monitored points have no observation within the last 30 minutes.")

expected = len(reference[reference.city.isin(selected_cities)])
trend = filtered.groupby(pd.Grouper(key="recorded_at", freq="1h")).agg(congestion_pct=("congestion_pct", "mean"), current_speed=("current_speed", "mean"))
with st.container(horizontal=True):
    st.metric("Average speed loss", f"{latest.congestion_pct.mean():.0f}%", border=True, chart_data=trend.congestion_pct, chart_type="area", help="Mean of 100 × (1 − current speed / free-flow speed), clipped to 0–100. Higher means slower traffic.")
    st.metric("Average road speed", f"{latest.current_speed.mean():.0f} km/h", border=True, chart_data=trend.current_speed)
    st.metric("Heavy or closed points", f"{latest.status.isin(['Heavy', 'Closed']).sum()} / {len(latest)}", border=True, help="Heavy: below 40% of free-flow speed. Closed: provider-reported road closure.")
    st.metric("Monitoring coverage", f"{len(latest)} / {expected}", border=True, help="Points with at least one observation inside the selected history window.")

if page == "Overview":
    main, detail = st.columns([2.4, 1], gap="medium")
    with main, st.container(border=True):
        st.subheader("The network at a glance")
        control, replay = st.columns([1, 2])
        with control:
            mode = st.segmented_control("Map view", ["3D columns", "2D points"], default="3D columns") or "3D columns"
        with replay:
            steps = sorted(filtered.recorded_at.dt.floor("15min").unique())
            replay_index = st.select_slider("Replay observations", options=list(range(len(steps))), value=len(steps) - 1, format_func=lambda i: pd.Timestamp(steps[i]).tz_convert("Africa/Casablanca").strftime("%d %b %H:%M"), disabled=len(steps) < 2, label_visibility="collapsed")
            replay_at = min(end, pd.Timestamp(steps[replay_index]) + pd.Timedelta(minutes=15) - pd.Timedelta(microseconds=1))
            st.caption(f"Snapshot at {replay_at.tz_convert('Africa/Casablanca'):%d %b · %H:%M}")
        current = snapshot(filtered, replay_at)
        selected = traffic_map(current, mode, replay_at)
        st.caption(":green[● Flowing]   :orange[● Moderate]   :red[● Heavy / closed]   · Gray: older than 30 min")
        st.caption("Drag to pan · scroll to zoom · shift-drag to rotate · click a column to inspect. Height shows speed loss, not building height.")
    with detail, st.container(border=True):
        st.subheader("Location spotlight")
        picked_city = selected.get("city") if selected else None
        candidates = current[current.city == picked_city] if picked_city in current.city.values else current
        focus_keys = candidates.location_key.tolist()
        picked_key = f"{picked_city}/{selected.get('label')}" if selected else None
        focus = st.selectbox("Monitoring point", focus_keys, index=focus_keys.index(picked_key) if picked_key in focus_keys else 0, format_func=lambda key: key.replace("/", " · "))
        row = candidates[candidates.location_key == focus].iloc[0]
        st.badge(row.status, color={"Flowing": "green", "Moderate": "orange", "Heavy": "red", "Closed": "red"}[row.status])
        st.metric("Observed speed", f"{row.current_speed:.0f} km/h")
        st.progress(float(min(1, max(0, row.congestion_ratio))), text=f"{min(100, row.congestion_ratio * 100):.0f}% of free-flow speed")
        st.caption(f"Free-flow baseline: {row.free_flow_speed:.0f} km/h")
        st.caption(f"Observed {row.recorded_at.tz_convert('Africa/Casablanca'):%d %b · %H:%M}")
        st.caption(f"Provider confidence: {row.confidence:.0%}" if pd.notna(row.confidence) else "Provider confidence unavailable")
        history = filtered[filtered.location_key == focus]
        st.altair_chart(trend_chart(history, by="location", height=145))
    left, right = st.columns([1.4, 1])
    with left, st.container(border=True):
        st.subheader("Traffic through the day")
        st.caption("Showing up to 12 cities with the highest average speed loss in this selection.")
        st.altair_chart(trend_chart(filtered))
    with right, st.container(border=True):
        st.subheader("City pulse")
        st.caption("Latest available observations · highest 15 city averages")
        st.altair_chart(city_chart(latest))

elif page == "Patterns":
    with st.container(border=True):
        st.subheader("Compare city trends")
        st.caption("Showing up to 12 cities with the highest average speed loss. Select fewer cities to compare a specific group.")
        measure = st.segmented_control("Measure", ["Speed loss", "Road speed"], default="Speed loss")
        st.altair_chart(trend_chart(filtered, metric="current_speed" if measure == "Road speed" else "congestion_pct", height=340))
    left, right = st.columns([1.5, 1])
    with left, st.container(border=True):
        st.subheader("The rush-hour fingerprint")
        hourly = filtered.assign(hour=filtered.recorded_at.dt.tz_convert("Africa/Casablanca").dt.hour).groupby(["city", "hour"], as_index=False).congestion_pct.mean()
        top_cities = filtered.groupby("city").congestion_pct.mean().nlargest(20).index
        hourly = hourly[hourly.city.isin(top_cities)]
        st.caption("Up to 20 cities with the highest average speed loss.")
        chart = alt.Chart(hourly).mark_rect(cornerRadius=3).encode(
            x=alt.X("hour:O", title="Hour · Africa/Casablanca"), y=alt.Y("city:N", title=None),
            color=alt.Color("congestion_pct:Q", title="Speed loss %", scale=alt.Scale(domain=[0, 100], range=["#153E48", "#2DD4BF", "#FBBF24", "#FB7185"])),
            tooltip=["city:N", "hour:O", alt.Tooltip("congestion_pct:Q", format=".1f", title="Speed loss %")]).properties(height=330)
        st.altair_chart(chart)
    with right, st.container(border=True):
        st.subheader("Slowest monitoring points")
        slow = filtered.groupby(["city", "location"], as_index=False).agg(congestion_pct=("congestion_pct", "mean"), current_speed=("current_speed", "mean")).sort_values("congestion_pct", ascending=False).head(10)
        st.dataframe(slow, hide_index=True, column_config={"city": "City", "location": "Point", "congestion_pct": st.column_config.ProgressColumn("Speed loss", min_value=0, max_value=100, format="%.0f%%"), "current_speed": st.column_config.NumberColumn("km/h", format="%.0f")})
        st.caption("Averages use observations in the selected window. Missing observations are not treated as free-flow traffic.")

else:
    view = st.segmented_control("Explore", ["Observations", "City catalog", "Pipeline health"], default="Observations")
    if view == "City catalog":
        st.subheader("A growing observation network")
        st.caption("Approximate district monitoring coordinates. Live road coverage depends on the provider response at each point.")
        st.dataframe(reference[reference.city.isin(selected_cities)].drop(columns="location_key"), hide_index=True, column_config={"city": "City", "region": "Region", "location": "Monitoring point", "lat": st.column_config.NumberColumn("Latitude", format="%.4f"), "lon": st.column_config.NumberColumn("Longitude", format="%.4f")})
    elif view == "Pipeline health":
        st.subheader("Quality at a glance")
        checks = pd.DataFrame({"Check": ["Missing speed values", "Duplicate point / time / source", "Invalid speed values", "Missing provider confidence"], "Rows": [int(filtered.current_speed.isna().sum()), int(filtered.duplicated(["location_key", "recorded_at", "source"]).sum()), int(((filtered.current_speed < 0) | (filtered.free_flow_speed <= 0)).sum()), int(filtered.confidence.isna().sum())]})
        st.dataframe(checks, hide_index=True)
        st.subheader("Collection runs")
        if source == "Demo playground":
            st.caption("The playground is generated in memory; no collection runs are written to your database.")
        else:
            try:
                with connection(configured_url()).connect() as conn:
                    runs = pd.read_sql(select(batches).where(batches.c.source == ("tomtom" if source == "Live database" else "demo")).order_by(batches.c.started_at.desc()).limit(50), conn)
                st.dataframe(runs, hide_index=True)
            except SQLAlchemyError:
                st.info("Collection history is unavailable. Initialize the current database schema.")
        st.caption("Storage model: cities → monitoring points → observations. Collection runs track successes and failures; original provider payloads are retained for audit.")
    else:
        st.subheader("Observation explorer")
        search = st.text_input("Find a monitoring point", placeholder="Search by city or district…")
        statuses = st.multiselect("Traffic condition", ["Flowing", "Moderate", "Heavy", "Closed"], default=["Flowing", "Moderate", "Heavy", "Closed"])
        export = filtered[filtered.status.isin(statuses)]
        if search:
            export = export[export.location_key.str.contains(search, case=False, regex=False)]
        st.caption(f"{len(export):,} matching observations · newest 1,000 shown")
        observation_table(export.sort_values("recorded_at", ascending=False).head(1000))
        st.download_button("Download filtered CSV", export.to_csv(index=False).encode("utf-8"), file_name="atlas-traffic-observations.csv", mime="text/csv", icon=":material/download:")

st.caption("ATLAS · Morocco mobility observatory / Congestion is measured as speed loss relative to free flow. Observations describe sampled road segments only.")
