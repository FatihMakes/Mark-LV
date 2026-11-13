"""
ISS (International Space Station) tracking and orbital prediction module for JARVIS Map Explorer.
Provides real-time telemetry from WhereTheISS API and calculated future orbital ground tracks.
"""

from __future__ import annotations

import collections
import datetime
import math
import threading
import time
from typing import Any, Dict, List, Optional
import requests

_ISS_API_URL = "https://api.wheretheiss.at/v1/satellites/25544"
_CACHE_TTL = 2.0  # seconds between API requests (respects WhereTheISS rate limits)

_lock = threading.Lock()
_last_fetch_time = 0.0
_cached_telemetry: Optional[Dict[str, Any]] = None
_history: collections.deque = collections.deque(maxlen=120)  # ~4 minutes of history


def get_iss_telemetry() -> Dict[str, Any]:
    """
    Fetch live ISS telemetry with 2-second rate-limiting cache and ring-buffer history.
    Returns:
        Dict containing latitude, longitude, altitude, velocity, visibility, timestamp,
        is_live status, and historical ground track.
    """
    global _last_fetch_time, _cached_telemetry

    now = time.monotonic()
    with _lock:
        if _cached_telemetry and (now - _last_fetch_time < _CACHE_TTL):
            result = dict(_cached_telemetry)
            result["history"] = list(_history)
            return result

    # Fetch live telemetry
    try:
        resp = requests.get(_ISS_API_URL, timeout=6.0, headers={"User-Agent": "JARVIS-MapExplorer/1.0"})
        if resp.status_code == 200:
            data = resp.json()
            lat = float(data.get("latitude", 0.0))
            lon = float(data.get("longitude", 0.0))
            alt = float(data.get("altitude", 420.0))  # km
            vel = float(data.get("velocity", 27600.0))  # km/h
            vis = str(data.get("visibility", "daylight"))
            footprint = float(data.get("footprint", 4500.0))
            solar_lat = float(data.get("solar_lat", 0.0))
            solar_lon = float(data.get("solar_lon", 0.0))
            ts = int(data.get("timestamp", time.time()))

            record = {
                "latitude": round(lat, 5),
                "longitude": round(lon, 5),
                "altitude_km": round(alt, 2),
                "altitude_miles": round(alt * 0.621371, 2),
                "velocity_kmh": round(vel, 1),
                "velocity_mph": round(vel * 0.621371, 1),
                "visibility": vis,
                "footprint_km": round(footprint, 1),
                "solar_lat": round(solar_lat, 4),
                "solar_lon": round(solar_lon, 4),
                "timestamp": ts,
                "iso_time": datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat(),
                "live": True,
                "source": "WhereTheISS API (api.wheretheiss.at)",
                "source_attribution": "Where The ISS At? API (Public Domain)",
                "data_freshness": "live",
            }

            with _lock:
                _last_fetch_time = now
                _cached_telemetry = record
                # Append to ground track history if moved
                if not _history or (_history[-1]["latitude"] != record["latitude"] or _history[-1]["longitude"] != record["longitude"]):
                    _history.append({
                        "latitude": record["latitude"],
                        "longitude": record["longitude"],
                        "timestamp": ts,
                        "altitude_km": record["altitude_km"],
                    })
                result = dict(record)
                result["history"] = list(_history)
                return result
        else:
            status_err = f"ISS API returned HTTP {resp.status_code}"
    except Exception as exc:
        status_err = f"ISS API network error: {exc}"

    # Fallback to cached telemetry or extrapolated fallback
    with _lock:
        if _cached_telemetry:
            res = dict(_cached_telemetry)
            res["live"] = False
            res["data_freshness"] = "cached"
            res["warning"] = status_err
            res["history"] = list(_history)
            return res

    # Extreme offline fallback: standard ISS orbital approximation
    epoch = time.time()
    res = {
        "latitude": 0.0,
        "longitude": 0.0,
        "altitude_km": 420.0,
        "altitude_miles": 260.9,
        "velocity_kmh": 27600.0,
        "velocity_mph": 17150.0,
        "visibility": "daylight",
        "footprint_km": 4500.0,
        "solar_lat": 0.0,
        "solar_lon": 0.0,
        "timestamp": int(epoch),
        "iso_time": datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).isoformat(),
        "live": False,
        "data_freshness": "unavailable",
        "warning": f"Live telemetry temporarily unavailable ({status_err})",
        "history": [],
    }
    return res


def calculate_orbital_prediction(
    current_lat: float,
    current_lon: float,
    minutes_ahead: int = 95,
    step_minutes: float = 1.0,
) -> Dict[str, Any]:
    """
    Calculate future orbital ground track for ISS for next N minutes (~1 full orbit).
    Uses orbital mechanics approximation:
    - Inclination: 51.64 degrees
    - Orbital period: ~92.9 minutes
    - Earth rotation: ~0.25068 degrees/minute westward relative to orbit
    Returns a GeoJSON FeatureCollection of predicted points and paths.
    """
    period_min = 92.9
    inc_rad = math.radians(51.64)

    # Approximate orbital phase (argument of latitude) from current latitude
    # sin(lat) = sin(inc) * sin(u)  =>  u = arcsin(clamp(sin(lat)/sin(inc), -1, 1))
    sin_inc = math.sin(inc_rad)
    sin_lat = math.sin(math.radians(current_lat))
    ratio = max(-1.0, min(1.0, sin_lat / sin_inc))
    u0 = math.asin(ratio)

    # Earth rotation rate relative to inertial orbit (360 degrees in 1436.07 minutes)
    earth_rot_deg_per_min = 360.0 / 1436.068

    points = []
    lines = []
    current_segment: List[List[float]] = []

    steps = int(minutes_ahead / step_minutes)
    for i in range(steps + 1):
        dt_min = i * step_minutes
        u = u0 + (2.0 * math.pi * (dt_min / period_min))

        pred_lat = math.degrees(math.asin(sin_inc * math.sin(u)))
        delta_lon = math.degrees(math.atan2(math.cos(inc_rad) * math.sin(u), math.cos(u)))
        pred_lon = (current_lon + delta_lon - (earth_rot_deg_per_min * dt_min)) % 360.0
        if pred_lon > 180.0:
            pred_lon -= 360.0

        coord = [round(pred_lon, 4), round(pred_lat, 4)]
        points.append({
            "coord": coord,
            "minutes_ahead": round(dt_min, 1),
            "lat": coord[1],
            "lon": coord[0],
        })

        # Check for antimeridian crossing (-180 / +180 wrap)
        if current_segment:
            prev_lon = current_segment[-1][0]
            if abs(coord[0] - prev_lon) > 180.0:
                lines.append(current_segment)
                current_segment = []
        current_segment.append(coord)

    if current_segment:
        lines.append(current_segment)

    return {
        "type": "FeatureCollection",
        "orbital_period_minutes": period_min,
        "inclination_degrees": 51.64,
        "is_prediction": True,
        "disclaimer": "Calculated future orbital projection, not live telemetry",
        "features": [
            {
                "type": "Feature",
                "properties": {"type": "orbit_path"},
                "geometry": {
                    "type": "MultiLineString",
                    "coordinates": lines,
                },
            }
        ],
        "waypoints": points[::5],  # sampled waypoints with timing
    }


def calculate_solar_terminator(epoch_time: Optional[float] = None) -> Dict[str, Any]:
    """
    Calculate the day/night solar terminator polygon for Earth mapping.
    Determines the shadow boundary based on the sun's subsolar coordinates.
    """
    if epoch_time is None:
        epoch_time = time.time()

    dt = datetime.datetime.fromtimestamp(epoch_time, tz=datetime.timezone.utc)
    # Day of year and fractional hour
    day_of_year = dt.timetuple().tm_yday
    hour = dt.hour + (dt.minute / 60.0) + (dt.second / 3600.0)

    # Approximate solar declination
    declination = math.radians(-23.44 * math.cos(math.radians(360.0 / 365.25 * (day_of_year + 10))))
    # Subsolar longitude (Greenwich hour angle)
    subsolar_lon = (12.0 - hour) * 15.0
    while subsolar_lon < -180.0:
        subsolar_lon += 360.0
    while subsolar_lon > 180.0:
        subsolar_lon -= 360.0

    # Generate terminator curve: points where solar elevation angle is 0
    # tan(lat) = -cos(lon - subsolar_lon) / tan(declination)
    coords = []
    tan_dec = math.tan(declination)
    if abs(tan_dec) < 1e-6:
        tan_dec = 1e-6

    for deg in range(-180, 181, 2):
        lon_rad = math.radians(deg - subsolar_lon)
        lat_rad = math.atan(-math.cos(lon_rad) / tan_dec)
        lat_deg = math.degrees(lat_rad)
        coords.append([round(float(deg), 2), round(lat_deg, 2)])

    # Construct night hemisphere polygon
    night_lat = -90.0 if declination > 0 else 90.0
    polygon_ring = list(coords)
    polygon_ring.append([180.0, night_lat])
    polygon_ring.append([-180.0, night_lat])
    polygon_ring.append(coords[0])

    return {
        "type": "FeatureCollection",
        "subsolar_point": {
            "latitude": round(math.degrees(declination), 3),
            "longitude": round(subsolar_lon, 3),
        },
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "layer": "day_night_terminator",
                    "night_pole": "south" if declination > 0 else "north",
                    "updated": dt.isoformat(),
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [polygon_ring],
                },
            }
        ],
    }
