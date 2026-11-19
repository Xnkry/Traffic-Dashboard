import math

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st


PALETTE = ["#2DD4BF", "#60A5FA", "#A78BFA", "#FBBF24", "#FB7185", "#38BDF8", "#FB923C", "#A3E635", "#E879F9", "#94A3B8", "#F472B6", "#818CF8"]


def map_data(frame, at):
    national = frame.city.nunique() > 1
    if national:
        points = frame.groupby("city", as_index=False).agg(
            lat=("lat", "mean"), lon=("lon", "mean"),
            congestion_pct=("congestion_pct", "mean"), current_speed=("current_speed", "mean"),
            recorded_at=("recorded_at", "min"), road_closure=("road_closure", "any"))
        points["label"] = points.city
    else:
        points = frame.copy()
        points["label"] = points.location
    points["color"] = points.apply(lambda r:
        [148, 163, 184, 190] if (at - r.recorded_at).total_seconds() > 1800 else
        [251, 113, 133, 230] if r.road_closure or r.congestion_pct > 60 else
        [251, 191, 36, 230] if r.congestion_pct > 30 else [45, 212, 191, 230], axis=1)
    points["elevation"] = points.congestion_pct.clip(lower=1) * (2200 if national else 12)
    points["speed"] = points.current_speed.round(0).astype(int)
    points["loss"] = points.congestion_pct.round(1)
    points["observed"] = points.recorded_at.dt.tz_convert("Africa/Casablanca").dt.strftime("%d %b %H:%M")
    return points[["city", "label", "lat", "lon", "color", "elevation", "speed", "loss", "observed"]], national


def traffic_map(frame, mode, at):
    points, national = map_data(frame, at)
    three_d = mode == "3D columns"
    span = max(float(points.lat.max() - points.lat.min()), float(points.lon.max() - points.lon.min()), .025)
    zoom = max(5, min(12, math.log2(200 / span)))
    layer = pdk.Layer("ColumnLayer" if three_d else "ScatterplotLayer",
        data=points.to_dict("records"), id="traffic", get_position="[lon, lat]",
        get_fill_color="color", get_elevation="elevation", elevation_scale=1,
        radius=7000 if national else 150, get_radius=7000 if national else 150,
        radius_min_pixels=7, radius_max_pixels=24, disk_resolution=32,
        extruded=three_d, pickable=True, auto_highlight=True)
    labels = pdk.Layer("TextLayer", id="labels", data=points.to_dict("records"),
        get_position="[lon, lat, elevation]" if three_d else "[lon, lat]",
        get_text="label", get_color=[226, 232, 240], get_size=11,
        get_pixel_offset=[0, -12], get_text_anchor="middle", get_alignment_baseline="bottom")
    deck = pdk.Deck(layers=[layer, labels], map_provider="carto", map_style=pdk.map_styles.DARK,
        initial_view_state=pdk.ViewState(latitude=float(points.lat.mean()), longitude=float(points.lon.mean()),
            zoom=zoom, pitch=48 if three_d else 0, bearing=-12 if three_d else 0),
        tooltip={"text": "{label}\nSpeed: {speed} km/h\nSpeed loss: {loss}%\nOldest contributing observation: {observed}"})
    event = st.pydeck_chart(deck, height=450, on_select="rerun", selection_mode="single-object", key=f"traffic_map_{national}_{mode}")
    objects = event.selection.get("objects", {}).get("traffic", [])
    return objects[0] if objects else None


def trend_chart(frame, by="city", metric="congestion_pct", height=240):
    values = frame.groupby([by, pd.Grouper(key="recorded_at", freq="1h")], as_index=False)[metric].mean()
    values["local_time"] = values.recorded_at.dt.tz_convert("Africa/Casablanca").dt.tz_localize(None)
    title = "Speed loss (%)" if metric == "congestion_pct" else "Speed (km/h)"
    return alt.Chart(values).mark_line(strokeWidth=2.5, point=values.local_time.nunique() < 3).encode(
        x=alt.X("local_time:T", title=None, axis=alt.Axis(format="%H:%M")),
        y=alt.Y(f"{metric}:Q", title=title if height > 150 else None, scale=alt.Scale(domain=[0, 100]) if metric == "congestion_pct" else alt.Scale(zero=True)),
        color=alt.Color(f"{by}:N", title=None, scale=alt.Scale(range=PALETTE), legend=alt.Legend(orient="bottom", columns=4) if height > 150 else None),
        tooltip=[alt.Tooltip(f"{by}:N", title="Location"), alt.Tooltip("local_time:T", title="Local time", format="%d %b %H:%M"), alt.Tooltip(f"{metric}:Q", title=title, format=".1f")]
    ).properties(height=height).interactive()


def city_chart(frame):
    values = frame.groupby("city", as_index=False).congestion_pct.mean()
    return alt.Chart(values).mark_bar(cornerRadiusEnd=4, size=12).encode(
        x=alt.X("congestion_pct:Q", title="Speed loss (%)", scale=alt.Scale(domain=[0, 100])),
        y=alt.Y("city:N", sort="-x", title=None),
        color=alt.Color("congestion_pct:Q", scale=alt.Scale(domain=[0, 30, 60, 100], range=["#2DD4BF", "#2DD4BF", "#FBBF24", "#FB7185"]), legend=None),
        tooltip=["city:N", alt.Tooltip("congestion_pct:Q", title="Speed loss (%)", format=".1f")]
    ).properties(height=280)


def observation_table(frame):
    columns = ["recorded_at", "city", "location", "status", "current_speed", "free_flow_speed", "congestion_pct", "confidence", "source"]
    st.dataframe(frame[columns], hide_index=True, height=430, column_config={
        "recorded_at": st.column_config.DatetimeColumn("Observed", format="DD MMM HH:mm", timezone="Africa/Casablanca"),
        "city": "City", "location": "Monitoring point", "status": "Condition", "source": "Source",
        "current_speed": st.column_config.NumberColumn("Speed", format="%.0f km/h"),
        "free_flow_speed": st.column_config.NumberColumn("Free flow", format="%.0f km/h"),
        "congestion_pct": st.column_config.ProgressColumn("Speed loss", min_value=0, max_value=100, format="%.0f%%"),
        "confidence": st.column_config.NumberColumn("Confidence", format="percent"),
    })
