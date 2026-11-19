import math

import numpy as np
import pandas as pd


INCIDENT_TYPES = {0: "Unknown", 1: "Accident", 2: "Fog", 3: "Dangerous conditions", 4: "Rain", 5: "Ice", 6: "Traffic jam", 7: "Lane closure", 8: "Road closure", 9: "Roadworks", 10: "Wind", 11: "Flooding", 14: "Broken-down vehicle"}


def distance_m(a, b):
    lat = math.radians((a[1] + b[1]) / 2)
    return math.hypot((a[0] - b[0]) * 111320 * math.cos(lat), (a[1] - b[1]) * 111320)


def point_line_distance(point, path):
    if not path:
        return float("inf")
    scale = 111320 * math.cos(math.radians(point[1]))
    values = [((p[0] - point[0]) * scale, (p[1] - point[1]) * 111320) for p in path]
    best = math.hypot(*values[0])
    for a, b in zip(values, values[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = dx * dx + dy * dy
        t = max(0, min(1, -(a[0] * dx + a[1] * dy) / length)) if length else 0
        best = min(best, math.hypot(a[0] + t * dx, a[1] + t * dy))
    return best


def split_path(path, max_length=400):
    if max_length <= 0:
        raise ValueError("Segment length must be positive")
    if len(path) < 2:
        return []
    parts, current, length = [], [path[0]], 0.0
    for target in path[1:]:
        start = current[-1]
        remaining = distance_m(start, target)
        while remaining > 1e-6 and length + remaining >= max_length:
            ratio = (max_length - length) / remaining
            cut = [start[0] + ratio * (target[0] - start[0]), start[1] + ratio * (target[1] - start[1])]
            current.append(cut)
            parts.append(current)
            current, length, start = [cut], 0.0, cut
            remaining = distance_m(start, target)
        if remaining > 1e-6:
            current.append(target)
            length += remaining
    if len(current) > 1:
        parts.append(current)
    return parts


def path_midpoint(path):
    total = sum(distance_m(a, b) for a, b in zip(path, path[1:]))
    target, walked = total / 2, 0.0
    for a, b in zip(path, path[1:]):
        length = distance_m(a, b)
        if walked + length >= target and length:
            fraction = (target - walked) / length
            return [a[0] + fraction * (b[0] - a[0]), a[1] + fraction * (b[1] - a[1])]
        walked += length
    return path[0]


def valid_path(path, minimum=2):
    return isinstance(path, list) and len(path) >= minimum and all(
        isinstance(p, (list, tuple)) and len(p) >= 2 and
        isinstance(p[0], (int, float)) and isinstance(p[1], (int, float)) and
        math.isfinite(p[0]) and math.isfinite(p[1]) and -180 <= p[0] <= 180 and -90 <= p[1] <= 90
        for p in path)


def nearby_incidents(path, incidents, threshold=60):
    matches = []
    for incident in incidents:
        geometry = incident.get("geometry", {})
        coords = geometry.get("coordinates", [])
        if geometry.get("type") == "Point":
            coords = [coords]
        if geometry.get("type") not in {"Point", "LineString"} or not valid_path(coords, 1):
            continue
        distance = min(point_line_distance(p, path) for p in coords)
        if len(coords) > 1:
            distance = min(distance, min(point_line_distance(p, coords) for p in path))
        if distance <= threshold:
            matches.append(incident)
    return matches


def baseline(history, segment_id, observed_at, provider_key):
    if history.empty:
        return None, 0
    prior = history[(history.segment_id == segment_id) & (history.status == "ok") &
                    (history.provider_key == provider_key)].copy()
    prior["observed_at"] = pd.to_datetime(prior.observed_at, utc=True)
    at = pd.Timestamp(observed_at)
    at = at.tz_localize("UTC") if at.tzinfo is None else at.tz_convert("UTC")
    local = at.tz_convert("Africa/Casablanca")
    times = prior.observed_at.dt.tz_convert("Africa/Casablanca")
    prior = prior[(times.dt.date < local.date()) &
                  (prior.observed_at >= at - pd.Timedelta(days=56)) &
                  (times.dt.dayofweek == local.dayofweek) & (times.dt.hour == local.hour) &
                  (~prior.road_closure.fillna(False).astype(bool)) & (prior.confidence.fillna(0) >= .5)]
    if prior.empty:
        return None, 0
    prior["day"] = prior.observed_at.dt.tz_convert("Africa/Casablanca").dt.date
    daily = prior.groupby("day").speed_loss.mean()
    return (float(daily.median()) if len(daily) >= 4 else None), len(daily)


def assess(roads, history, poll, now=None):
    now = pd.Timestamp(now or pd.Timestamp.now(tz="UTC"))
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    current = history.copy()
    if not current.empty:
        current["observed_at"] = pd.to_datetime(current.observed_at, utc=True)
        current = current[current.observed_at <= now].sort_values("observed_at").drop_duplicates("segment_id", keep="last").set_index("segment_id")
    poll_time = pd.to_datetime(poll["observed_at"], utc=True) if poll else None
    incident_fresh = bool(poll and poll["success"] and 0 <= (now - poll_time).total_seconds() <= 1800)
    incidents = poll["payload"] if incident_fresh else []
    result = []
    for road in roads.to_dict("records"):
        row = {**road, "condition": "Unobserved", "evidence": "No current observation", "speed_loss": None,
               "current_speed": None, "baseline_loss": None, "baseline_days": 0, "anomaly_pp": None,
               "incident_count": 0, "accident_count": 0, "observed_at": None, "color": [100, 116, 139],
               "flow_geometry": [], "incident_feed": "Available" if incident_fresh else "Unknown / stale"}
        if current.empty or road["id"] not in current.index:
            result.append(row)
            continue
        reading = current.loc[road["id"]]
        row["observed_at"] = reading.observed_at
        if (now - reading.observed_at).total_seconds() > 1800 or reading.status != "ok":
            row.update(condition="Stale" if reading.status == "ok" else "Unavailable", evidence="No reliable current speed")
            result.append(row)
            continue
        if pd.isna(reading.confidence) or reading.confidence < .5:
            row.update(condition="Low confidence", evidence="Provider confidence below 50% or unavailable")
            result.append(row)
            continue
        loss = float(reading.speed_loss)
        typical, days = baseline(history, road["id"], reading.observed_at, reading.provider_key)
        aligned = incident_fresh and abs((reading.observed_at - poll_time).total_seconds()) <= 900
        matches = nearby_incidents(reading.geometry, incidents) if aligned else []
        accidents = sum(i["properties"].get("iconCategory") == 1 for i in matches)
        labels = sorted({INCIDENT_TYPES.get(i["properties"].get("iconCategory"), "Other") for i in matches})
        evidence = "Nearby report: " + ", ".join(labels) if matches else "No nearby reported incident" if aligned else "Incident feed unavailable or not time-aligned"
        if loss >= 30 and not matches and aligned:
            if typical is None:
                evidence = "Congestion; insufficient history to classify pattern"
            elif loss - typical >= 20:
                evidence = "Unusual slowdown; cause unconfirmed"
            elif typical >= 30 and abs(loss - typical) <= 15:
                evidence = "Consistent with recurring congestion; cause unconfirmed"
            else:
                evidence = "Congestion; cause unconfirmed"
        condition = "Closed" if reading.road_closure else "Heavy" if loss > 60 else "Moderate" if loss > 30 else "Flowing"
        row.update(condition=condition, evidence=evidence, speed_loss=loss, current_speed=float(reading.current_speed),
                   baseline_loss=typical, baseline_days=days, anomaly_pp=loss - typical if typical is not None else None,
                   incident_count=len(matches), accident_count=accidents, flow_geometry=reading.geometry,
                   color=[251, 113, 133] if condition in {"Heavy", "Closed"} else [251, 191, 36] if condition == "Moderate" else [45, 212, 191])
        result.append(row)
    return pd.DataFrame(result)
