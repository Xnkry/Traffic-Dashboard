import math

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from road_analysis import INCIDENT_TYPES, assess
from road_store import areas, load_city, load_incident_history
from traffic_data import make_engine


@st.cache_resource(ttl=3600, max_entries=4)
def road_connection(url):
    return make_engine(url)


@st.cache_data(ttl=30, max_entries=8)
def road_city_data(url, city):
    return (*load_city(road_connection(url), city), load_incident_history(road_connection(url), city))


def render(url, selected_cities, source):
    st.title("Avenues, accidents & traffic patterns")
    st.caption("Road-segment observations and reported incidents. Congestion alone does not establish its cause.")
    if source != "Live database":
        st.info("Choose Live database to study real avenues and incident reports. Synthetic city samples are excluded from this workspace.")
        return
    try:
        with road_connection(url).connect() as conn:
            available = list(conn.scalars(select(areas.c.city).order_by(areas.c.city)))
    except SQLAlchemyError:
        available = []
    eligible = [city for city in available if city in selected_cities]
    if not eligible:
        st.info("No imported road inventory for the selected cities. Import a study area first; city-center samples do not cover avenues.")
        city = selected_cities[0] if selected_cities else "Casablanca"
        st.code(f'uv run --env-file .env python road_ingest.py import --city "{city}"', language="bash")
        return
    city = st.selectbox("Study city", eligible)
    if st.button("Refresh road evidence", icon=":material/refresh:"):
        road_city_data.clear()
    try:
        roads, history, poll, area, incident_history = road_city_data(url, city)
    except SQLAlchemyError:
        st.error("Road observations are unavailable. Check the database and initialize the road schema.")
        return
    now = pd.Timestamp.now(tz="UTC")
    assessed = assess(roads, history, poll, now)
    if assessed.empty:
        st.info("The imported area has no active road samples.")
        return
    fresh = assessed.condition.isin(["Flowing", "Moderate", "Heavy", "Closed"])
    feed_fresh = bool(poll and poll["success"] and 0 <= (now - pd.to_datetime(poll["observed_at"], utc=True)).total_seconds() <= 1800)
    incidents = poll["payload"] if feed_fresh else []
    with st.container(horizontal=True):
        st.metric("Fresh road samples", f"{fresh.sum()} / {len(assessed)}", border=True)
        st.metric("Heavy or closed samples", int(assessed.condition.isin(["Heavy", "Closed"]).sum()), border=True)
        st.metric("Reported accidents in study area", sum(i["properties"]["iconCategory"] == 1 for i in incidents) if feed_fresh else "Unknown", border=True)
        st.metric("Road names in inventory", int(roads.loc[~roads.name.str.startswith("Unnamed road"), "name"].nunique()), border=True)
    if area.get("scope") == "administrative":
        st.caption(f"OSM administrative extraction: {area.get('boundary_name', city)} · area {area.get('boundary_id')}. All eligible mapped roads are requested, including unnamed roads; OSM completeness is not guaranteed. Boundary-crossing ways may extend beyond the city.")
    elif area.get("scope") == "file":
        st.caption("Road inventory from a supplied network file. Geographic completeness depends on that file.")
    else:
        st.caption(f"Bounded study: rectangle ±{area['radius_km']:g} km from the catalog center; not the full administrative city boundary.")
    st.caption("Long OSM ways are sampled in pieces of up to 400 m. Fresh sample count is not full-road or all-direction coverage.")
    if not feed_fresh:
        st.warning("The incident feed is missing, failed, or older than 30 minutes. Accident status is unknown.")
    else:
        st.caption(f"Incident feed checked {pd.to_datetime(poll['observed_at'], utc=True).tz_convert('Africa/Casablanca'):%d %b %H:%M}. An empty feed means no incidents reported by this provider, not proof that none exist.")
    with st.expander("Plan network freshness and API usage"):
        batch_size = st.number_input("Road samples per batch", min_value=1, max_value=10000, value=50)
        minutes = st.number_input("Minutes between batches", min_value=1, max_value=1440, value=5)
        sweep = math.ceil(len(roads) / batch_size) * minutes
        st.caption(f"At this budget, a full rotation takes approximately {sweep:,} minutes. Estimated calls/day: {(min(batch_size, len(roads)) + 1) * 1440 / minutes:,.0f} before retries, assuming one incident request per batch. These planning controls do not change the collector.")
        if sweep > 30:
            st.warning("This budget cannot keep the entire imported network fresh within 30 minutes. Increase the allowance, narrow the study, or use a licensed network-wide traffic feed. Unsampled roads remain unknown.")
    avenue = st.selectbox("Avenue / road", ["All roads", "Unnamed roads", *sorted(roads.loc[~roads.name.str.startswith("Unnamed road"), "corridor_label"].unique())])
    visible = assessed if avenue == "All roads" else assessed[assessed.name.str.startswith("Unnamed road")] if avenue == "Unnamed roads" else assessed[assessed.corridor_label == avenue]
    st.caption(f"{len(visible):,} imported samples in the road selection; unknown road names are kept separately rather than invented.")
    if avenue not in {"All roads", "Unnamed roads"}:
        corridor_id = visible.iloc[0].corridor_id
        st.caption(f"Corridor ID: {corridor_id}. Disconnected roads with the same name are kept separate.")
    search = st.text_input("Find a road", placeholder="Avenue, boulevard, street name…")
    if search:
        visible = visible[visible.name.str.contains(search, case=False, regex=False)]
    if visible.empty:
        st.info("No roads match your search.")
        return
    detail = st.segmented_control("Map detail", ["Major roads", "All selected roads", "Observed samples"], default="Major roads")
    mapped = visible
    if avenue == "All roads" and detail == "Major roads":
        mapped = visible[visible.road_class.isin(["motorway", "trunk", "primary", "secondary", "tertiary", "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"])]
    elif detail == "Observed samples":
        mapped = visible[visible.condition.isin(["Flowing", "Moderate", "Heavy", "Closed"])]
    st.caption(f"Map displays {len(mapped):,} of {len(visible):,} selected samples. Inventory and coverage totals still include the whole selection.")
    base = mapped[["name", "geometry"]].to_dict("records")
    measured = mapped[mapped.condition.isin(["Flowing", "Moderate", "Heavy", "Closed"])].copy()
    measured["tooltip"] = measured.apply(lambda r: f"{r['name']} · {r['condition']} · {r['speed_loss']:.0f}% speed loss", axis=1)
    layers = [pdk.Layer("PathLayer", id="inventory", data=base, get_path="geometry", get_color=[88, 105, 127], get_width=2, width_units=pdk.types.String("pixels"), pickable=True)]
    layers.append(pdk.Layer("PathLayer", id="observed", data=measured[["name", "flow_geometry", "color", "tooltip"]].to_dict("records"), get_path="flow_geometry", get_color="color", get_width=5, width_units=pdk.types.String("pixels"), pickable=True))
    incident_points = []
    for item in incidents:
        geometry = item["geometry"]
        coords = geometry["coordinates"]
        point = coords if geometry["type"] == "Point" else coords[len(coords) // 2]
        kind = INCIDENT_TYPES.get(item["properties"]["iconCategory"], "Other incident")
        incident_points.append({"position": point, "name": kind, "tooltip": kind + " · provider report; association does not prove cause", "color": [251, 113, 133] if kind == "Accident" else [251, 191, 36]})
    layers.append(pdk.Layer("ScatterplotLayer", id="incidents", data=incident_points, get_position="position", get_fill_color="color", get_radius=30, radius_min_pixels=6, pickable=True))
    deck = pdk.Deck(layers=layers, map_style=pdk.map_styles.DARK, map_provider="carto",
        initial_view_state=pdk.ViewState(latitude=float(visible.lat.mean()), longitude=float(visible.lon.mean()), zoom=12 if avenue == "All roads" else 14), tooltip={"text": "{name}\n{tooltip}"})
    st.pydeck_chart(deck, height=500)
    st.caption(":green[Flowing] · :orange[Moderate] · :red[Heavy / closed] · Gray: imported inventory without current reliable flow. Colored paths show provider geometry, which can differ from OSM and may represent one direction only.")
    st.markdown("Road inventory © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), ODbL. Flow and incident reports: TomTom.")
    coverage = visible.assign(fresh=visible.condition.isin(["Flowing", "Moderate", "Heavy", "Closed"]))
    with st.expander("Coverage by avenue / road name"):
        summary = coverage.groupby("corridor_label", as_index=False).agg(imported_samples=("id", "size"), fresh_samples=("fresh", "sum"))
        summary["sample_coverage_pct"] = 100 * summary.fresh_samples / summary.imported_samples
        st.dataframe(summary, hide_index=True, column_config={"corridor_label": "Road corridor", "imported_samples": "Imported samples", "fresh_samples": "Fresh samples", "sample_coverage_pct": st.column_config.ProgressColumn("Fresh sample coverage", min_value=0, max_value=100, format="%.0f%%")})
    st.subheader("What the evidence says")
    st.caption("A report within approximately 60 m of the observed segment is shown as nearby evidence only. Parallel roads, junctions, and direction differences can produce false associations. Flow and incident checks must be within 15 minutes of each other.")
    display = visible[["corridor_label", "condition", "current_speed", "speed_loss", "evidence", "accident_count", "baseline_loss", "baseline_days", "anomaly_pp", "observed_at"]].copy()
    st.caption(f"{len(display):,} matching samples; table shows up to 1,000, with recent observations first. The download includes the full selection.")
    display = display.sort_values("observed_at", ascending=False, na_position="last")
    st.dataframe(display.head(1000), hide_index=True, column_config={
        "corridor_label": "Road corridor", "condition": "Condition", "evidence": "Evidence / pattern", "accident_count": "Nearby accident reports",
        "current_speed": st.column_config.NumberColumn("Speed", format="%.0f km/h"),
        "speed_loss": st.column_config.ProgressColumn("Speed loss", min_value=0, max_value=100, format="%.0f%%"),
        "baseline_loss": st.column_config.NumberColumn("Typical loss", format="%.0f%%"),
        "baseline_days": "Comparable days", "anomaly_pp": st.column_config.NumberColumn("Above typical", format="%.0f pp"),
        "observed_at": st.column_config.DatetimeColumn("Last attempt", timezone="Africa/Casablanca", format="DD MMM HH:mm")})
    st.download_button("Download avenue evidence", display.to_csv(index=False).encode(), "avenue-evidence.csv", "text/csv")
    if feed_fresh:
        with st.expander("Reported incidents in the study area"):
            details = [{"Type": INCIDENT_TYPES.get(i["properties"]["iconCategory"], "Other"), "From": i["properties"].get("from"), "To": i["properties"].get("to"), "Description": "; ".join(e.get("description", "") for e in i["properties"].get("events", [])), "Provider ID": i["properties"]["id"]} for i in incidents]
            st.dataframe(pd.DataFrame(details), hide_index=True)
    with st.expander("Incident reporting history · entire study area"):
        if incident_history.empty:
            st.caption("No incident polls have been recorded.")
        else:
            daily = {}
            for check in incident_history.itertuples():
                day = pd.to_datetime(check.observed_at, utc=True).tz_convert("Africa/Casablanca").date()
                bucket = daily.setdefault(day, {"Date": day, "Successful checks": 0, "Failed checks": 0, "Accident IDs": set()})
                bucket["Successful checks" if check.success else "Failed checks"] += 1
                if check.success:
                    bucket["Accident IDs"].update(i["properties"]["id"] for i in check.payload if i["properties"]["iconCategory"] == 1)
            summaries = [{"Date": b["Date"], "Distinct reported accidents": len(b["Accident IDs"]) if b["Successful checks"] else None,
                "Successful checks": b["Successful checks"], "Failed checks": b["Failed checks"]} for b in daily.values()]
            st.dataframe(pd.DataFrame(summaries), hide_index=True)
            st.caption("Distinct provider IDs observed per day, not accident occurrence counts. The same ongoing report may appear on multiple days; polling gaps and unreported accidents remain unknown. This table covers the whole study area, regardless of the road filter.")
    st.subheader("Avenue patterns over time")
    if history.empty:
        st.info("No road history yet. Run the road collector regularly to build the analysis.")
        return
    values = history[(history.segment_id.isin(visible.id)) & (history.status == "ok") & (history.confidence.fillna(0) >= .5)].copy()
    if values.empty:
        st.info("No reliable flow observations for these roads yet.")
        return
    names = roads.set_index("id").corridor_label
    values["road"] = values.segment_id.map(names)
    values["observed_at"] = pd.to_datetime(values.observed_at, utc=True)
    values["local_time"] = values.observed_at.dt.tz_convert("Africa/Casablanca").dt.tz_localize(None)
    values["hour"] = values.local_time.dt.hour
    values["weekday"] = values.local_time.dt.day_name()
    top = values.groupby("road").speed_loss.mean().nlargest(12).index
    hourly = values[values.road.isin(top)].groupby(["road", pd.Grouper(key="local_time", freq="1h")], as_index=False).speed_loss.mean()
    if len(hourly) > 4500:
        hourly = values[values.road.isin(top)].groupby(["road", pd.Grouper(key="local_time", freq="6h")], as_index=False).speed_loss.mean()
    st.caption("Up to 12 roads with the highest observed mean speed loss. Averages describe collected samples, not full avenue coverage or vehicle counts.")
    st.altair_chart(alt.Chart(hourly).mark_line(point=True).encode(x=alt.X("local_time:T", title="Local time"), y=alt.Y("speed_loss:Q", title="Speed loss (%)", scale=alt.Scale(domain=[0, 100])), color="road:N", tooltip=["road", "local_time:T", alt.Tooltip("speed_loss:Q", format=".1f")]).properties(height=260))
    heat = values.groupby(["weekday", "hour"], as_index=False).agg(speed_loss=("speed_loss", "mean"), samples=("speed_loss", "size"))
    st.altair_chart(alt.Chart(heat).mark_rect().encode(x=alt.X("hour:O", title="Hour"), y=alt.Y("weekday:N", sort=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"], title=None), color=alt.Color("speed_loss:Q", scale=alt.Scale(domain=[0, 100], scheme="yelloworangered")), tooltip=["weekday", "hour", "samples", alt.Tooltip("speed_loss:Q", format=".1f")]).properties(height=220))
    st.caption("Baseline: median of daily mean speed loss for the same sample, provider segment, weekday and hour over the preceding 56 days. Requires at least four distinct previous dates. A recurring pattern is not proof of excessive demand; speed data cannot measure road occupancy or establish causality.")
