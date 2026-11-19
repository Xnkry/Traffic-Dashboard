import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from motorway import STUDY
from road_analysis import assess, INCIDENT_TYPES
from road_dashboard import road_city_data


def render(url, source):
    st.title("Casablanca–Marrakech · motorway study")
    st.caption("A3 main carriageways · speed, congestion and reported incidents")
    if source != "Live database":
        st.info("Choose Live database to view motorway evidence.")
        return
    if st.button("Refresh motorway evidence", icon=":material/refresh:"):
        road_city_data.clear()
    try:
        roads, history, poll, area, incident_history = road_city_data(url, STUDY)
    except SQLAlchemyError:
        roads = pd.DataFrame()
    if roads.empty:
        st.info("Import the motorway inventory to start this study.")
        st.code(f'uv run --env-file .env python road_ingest.py import --city "{STUDY}"', language="bash")
        return
    now = pd.Timestamp.now(tz="UTC")
    data = assess(roads, history, poll, now)
    fresh = data.condition.isin(["Flowing", "Moderate", "Heavy", "Closed"])
    feed_ok = bool(poll and poll["success"] and 0 <= (now-pd.to_datetime(poll["observed_at"], utc=True)).total_seconds() <= 1800)
    incidents = poll["payload"] if feed_ok else []
    with st.container(horizontal=True):
        st.metric("Fresh speed samples", f"{fresh.sum():,} / {len(data):,}", border=True)
        st.metric("Heavy / closed samples", int(data.condition.isin(["Heavy", "Closed"]).sum()), border=True)
        st.metric("Reported accidents near motorway", sum(i["properties"]["iconCategory"] == 1 for i in incidents) if feed_ok else "Unknown", border=True)
    st.caption("Bounded A3 inventory between 31.68°N and 33.58°N, including mapped main carriageways in both directions. Ramps and city streets are excluded. Endpoints are study limits, not official motorway kilometre posts. Map completeness depends on OpenStreetMap.")
    if not feed_ok:
        st.warning("Incident checks are incomplete, missing or older than 30 minutes. Accident status is unknown.")
    else:
        st.caption(f"Incident check: {pd.to_datetime(poll['observed_at'], utc=True).tz_convert('Africa/Casablanca'):%d %b %H:%M}. Reports within approximately 60 m of mapped motorway geometry; nearby roads may still be included.")
    section = st.slider("Study latitude range · south (Marrakech) to north (Casablanca)", min_value=31.68, max_value=33.58, value=(31.68, 33.58), step=.01)
    visible = data[data.lat.between(*section)].copy()
    only_accidents = st.toggle("Show only accident reports", value=False)
    if visible.empty:
        st.info("No mapped samples in this section.")
        return
    visible["tooltip"] = visible.apply(lambda r: f"{r['name']} · {r['condition']}" + (f" · {r['current_speed']:.0f} km/h · {r['speed_loss']:.0f}% speed loss" if pd.notna(r['current_speed']) else " · speed unknown"), axis=1)
    measured = visible[visible.condition.isin(["Flowing", "Moderate", "Heavy", "Closed"])]
    markers = []
    for item in incidents:
        props, geometry = item["properties"], item["geometry"]
        if only_accidents and props["iconCategory"] != 1:
            continue
        coords = geometry["coordinates"]
        point = coords if geometry["type"] == "Point" else coords[len(coords)//2]
        if not section[0] <= point[1] <= section[1]:
            continue
        label = INCIDENT_TYPES.get(props["iconCategory"], "Other incident")
        markers.append({"position": point, "tooltip": f"{label} · {props.get('from', '')} → {props.get('to', '')} · provider report", "color": [255, 70, 90] if props["iconCategory"] == 1 else [251, 191, 36]})
    layers = [pdk.Layer("PathLayer", id="motorway-inventory", data=visible.to_dict("records"), get_path="geometry", get_color=[100,116,139], get_width=3, width_units=pdk.types.String("pixels"), pickable=True),
        pdk.Layer("PathLayer", id="motorway-speeds", data=measured.to_dict("records"), get_path="flow_geometry", get_color="color", get_width=6, width_units=pdk.types.String("pixels"), pickable=True),
        pdk.Layer("ScatterplotLayer", id="motorway-incidents", data=markers, get_position="position", get_fill_color="color", get_radius=100, radius_min_pixels=7, pickable=True)]
    st.pydeck_chart(pdk.Deck(layers=layers, map_style=pdk.map_styles.DARK, map_provider="carto", initial_view_state=pdk.ViewState(latitude=float(visible.lat.mean()), longitude=float(visible.lon.mean()), zoom=7 if section[1]-section[0] > 1 else 9), tooltip={"text": "{tooltip}"}), height=600)
    st.caption("Teal: flowing · amber: moderate · red: heavy/closed · gray: no reliable current speed. Hover paths and markers for details. Speeds describe provider segments, not individual vehicles. Direction matching is not guaranteed on parallel carriageways.")
    st.markdown("Roads © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), ODbL · Traffic reports: TomTom")
    columns = ["id", "lat", "lon", "condition", "current_speed", "speed_loss", "evidence", "baseline_loss", "baseline_days", "observed_at"]
    st.subheader("Slowdown locations")
    table = visible[columns].sort_values("speed_loss", ascending=False, na_position="last")
    st.dataframe(table.head(1000), hide_index=True, column_config={"current_speed": st.column_config.NumberColumn("Speed (km/h)", format="%.0f"), "speed_loss": st.column_config.NumberColumn("Loss vs free flow (%)", format="%.1f"), "observed_at": st.column_config.DatetimeColumn("Last attempt", timezone="Africa/Casablanca")})
    st.download_button("Download motorway evidence", table.to_csv(index=False).encode(), "motorway-evidence.csv", "text/csv")
    if markers:
        st.subheader("Reported incident locations")
        st.dataframe(pd.DataFrame([{"Report": m["tooltip"], "Longitude": m["position"][0], "Latitude": m["position"][1]} for m in markers]), hide_index=True)
    st.subheader("Speed study over time")
    if history.empty:
        st.info("Run the collector regularly to build speed history.")
        return
    values = history[(history.segment_id.isin(visible.id)) & (history.status == "ok") & (history.confidence.fillna(0) >= .5)].copy()
    if values.empty:
        st.info("No reliable speed observations for this section yet.")
        return
    values["local_time"] = pd.to_datetime(values.observed_at, utc=True).dt.tz_convert("Africa/Casablanca").dt.tz_localize(None)
    values["latitude_band"] = (values.segment_id.map(roads.set_index("id").lat) * 20).round() / 20
    hourly = values.groupby(["latitude_band", pd.Grouper(key="local_time", freq="12h")], as_index=False).agg(speed=("current_speed", "median"), free_flow=("free_flow_speed", "median"), loss=("speed_loss", "median"), readings=("id", "size"))
    st.altair_chart(alt.Chart(hourly).mark_rect().encode(x=alt.X("local_time:T", title="Local time · 12-hour bins"), y=alt.Y("latitude_band:O", title="Latitude band (north ↑)", sort="descending"), color=alt.Color("loss:Q", title="Median speed loss (%)", scale=alt.Scale(domain=[0,100], scheme="yelloworangered")), tooltip=["local_time:T", "latitude_band", "speed", "free_flow", "loss", "readings"]).properties(height=400))
    selected = st.selectbox("Inspect a speed sampling point", sorted(values.segment_id.unique()))
    series = values[values.segment_id == selected].groupby(pd.Grouper(key="local_time", freq="1h"), as_index=False).agg(current_speed=("current_speed", "median"), free_flow_speed=("free_flow_speed", "median"))
    st.line_chart(series.set_index("local_time")[["current_speed", "free_flow_speed"]], y_label="km/h")
    st.caption("Historical charts include reliable observed samples only; blank cells mean no observations. Latitude bins combine carriageways and are not distance markers. Repeated provider segments may appear at several sampling points. Free-flow speed is a comparison baseline, not a posted speed limit. Speed loss cannot establish vehicle volume, road occupancy, or accident causation.")
    st.caption("Recurring-pattern comparison requires four distinct previous dates for the same sample, provider segment, weekday and hour. Continue collection over several weeks before interpreting weekly patterns.")
