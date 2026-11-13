"""
Live Flight Tracking module for JARVIS Map Explorer.
Queries OpenSky Network API with viewport filtering, OAuth2 credentials support,
rate-limiting cache, and aircraft detail parsing.
"""

from __future__ import annotations

import datetime
import math
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import requests

_OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
_OPENSKY_TRACKS_URL = "https://opensky-network.org/api/tracks/all"
_OPENSKY_AUTH_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"

_lock = threading.Lock()
_cache_by_bbox: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_oauth_token: Optional[str] = None
_oauth_token_expiry: float = 0.0
_last_request_time: float = 0.0
_MIN_INTERVAL_SEC = 6.0  # Respect OpenSky anonymous and authenticated limits


def _get_auth_header(client_id: str = "", client_secret: str = "") -> Dict[str, str]:
    """Retrieve or refresh OpenSky OAuth2 token if credentials are provided."""
    global _oauth_token, _oauth_token_expiry

    if not client_id or not client_secret:
        return {}

    now = time.time()
    with _lock:
        if _oauth_token and now < _oauth_token_expiry - 60:
            return {"Authorization": f"Bearer {_oauth_token}"}

    try:
        data = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        resp = requests.post(_OPENSKY_AUTH_URL, data=data, timeout=8.0)
        if resp.status_code == 200:
            token_data = resp.json()
            token = token_data.get("access_token")
            expires_in = int(token_data.get("expires_in", 1800))
            with _lock:
                _oauth_token = token
                _oauth_token_expiry = now + expires_in
            return {"Authorization": f"Bearer {token}"}
    except Exception as exc:
        print(f"[MapExplorer Flights] OpenSky OAuth2 token error: {exc}")

    return {}


def query_flights(
    lamin: Optional[float] = None,
    lomin: Optional[float] = None,
    lamax: Optional[float] = None,
    lomax: Optional[float] = None,
    callsign: Optional[str] = None,
    icao24: Optional[str] = None,
    client_id: str = "",
    client_secret: str = "",
    limit: int = 250,
) -> Dict[str, Any]:
    """
    Query live aircraft positions from OpenSky Network.
    Enforces viewport bounds, rate-limits queries, and caches regional responses.
    """
    global _last_request_time

    # Validate and normalize bounding box
    bbox_key = "global"
    params: Dict[str, Any] = {}
    if all(v is not None for v in (lamin, lomin, lamax, lomax)):
        # Clamp to geographic ranges
        lamin_c = max(-90.0, min(90.0, float(lamin)))  # type: ignore
        lamax_c = max(-90.0, min(90.0, float(lamax)))  # type: ignore
        lomin_c = max(-180.0, min(180.0, float(lomin)))  # type: ignore
        lomax_c = max(-180.0, min(180.0, float(lomax)))  # type: ignore

        if lamin_c > lamax_c:
            lamin_c, lamax_c = lamax_c, lamin_c
        if lomin_c > lomax_c:
            lomin_c, lomax_c = lomax_c, lomin_c

        # Quantize bounding box to 0.25 degree grid for aggressive caching
        q_lamin = round(lamin_c * 4) / 4
        q_lamax = round(lamax_c * 4) / 4
        q_lomin = round(lomin_c * 4) / 4
        q_lomax = round(lomax_c * 4) / 4

        bbox_key = f"{q_lamin}_{q_lomin}_{q_lamax}_{q_lomax}"
        params["lamin"] = lamin_c
        params["lomin"] = lomin_c
        params["lamax"] = lamax_c
        params["lomax"] = lomax_c

    now = time.monotonic()
    with _lock:
        if bbox_key in _cache_by_bbox:
            cached_time, cached_data = _cache_by_bbox[bbox_key]
            if (now - cached_time) < _MIN_INTERVAL_SEC:
                filtered = _filter_aircraft(cached_data, callsign, icao24, limit)
                return {
                    "aircraft": filtered,
                    "count": len(filtered),
                    "total_in_view": len(cached_data),
                    "timestamp": int(time.time()),
                    "live": True,
                    "data_freshness": "cached",
                    "source": "OpenSky Network (cached)",
                    "attribution": "Data provided by The OpenSky Network (https://opensky-network.org)",
                }

    # Throttle requests
    with _lock:
        elapsed = now - _last_request_time
        if elapsed < 2.0:
            time.sleep(2.0 - elapsed)
        _last_request_time = time.monotonic()

    headers = {"User-Agent": "JARVIS-Mark-LVII-MapExplorer/1.0"}
    auth_header = _get_auth_header(client_id, client_secret)
    headers.update(auth_header)

    try:
        resp = requests.get(_OPENSKY_STATES_URL, params=params, headers=headers, timeout=8.0)
        if resp.status_code == 200:
            payload = resp.json()
            time_epoch = int(payload.get("time", time.time()))
            states = payload.get("states") or []

            aircraft_list: List[Dict[str, Any]] = []
            for s in states:
                # OpenSky state vector format:
                # 0: icao24, 1: callsign, 2: origin_country, 3: time_position, 4: last_contact,
                # 5: longitude, 6: latitude, 7: baro_altitude, 8: on_ground, 9: velocity (m/s),
                # 10: true_track (deg), 11: vertical_rate (m/s), 12: sensors, 13: geo_altitude,
                # 14: squawk, 15: spi, 16: position_source
                if len(s) < 11:
                    continue
                lon = s[5]
                lat = s[6]
                if lon is None or lat is None:
                    continue

                c_sign = (s[1] or "").strip()
                baro_alt_m = s[7]
                speed_ms = s[9]
                heading = s[10]
                v_rate_ms = s[11]

                alt_ft = round(baro_alt_m * 3.28084) if baro_alt_m is not None else None
                alt_m = round(baro_alt_m) if baro_alt_m is not None else None
                speed_kts = round(speed_ms * 1.94384) if speed_ms is not None else None
                speed_kmh = round(speed_ms * 3.6) if speed_ms is not None else None
                v_rate_fpm = round(v_rate_ms * 196.85) if v_rate_ms is not None else None

                item = {
                    "icao24": str(s[0]).lower(),
                    "callsign": c_sign or "UNTITLED",
                    "origin_country": str(s[2] or "Unknown"),
                    "latitude": round(float(lat), 5),
                    "longitude": round(float(lon), 5),
                    "altitude_feet": alt_ft,
                    "altitude_meters": alt_m,
                    "speed_knots": speed_kts,
                    "speed_kmh": speed_kmh,
                    "heading": round(float(heading), 1) if heading is not None else 0.0,
                    "vertical_rate_fpm": v_rate_fpm,
                    "on_ground": bool(s[8]),
                    "last_contact": int(s[4]) if s[4] else time_epoch,
                    "squawk": str(s[14]) if len(s) > 14 and s[14] else None,
                }
                aircraft_list.append(item)

            with _lock:
                _cache_by_bbox[bbox_key] = (time.monotonic(), aircraft_list)

            filtered = _filter_aircraft(aircraft_list, callsign, icao24, limit)
            return {
                "aircraft": filtered,
                "count": len(filtered),
                "total_in_view": len(aircraft_list),
                "timestamp": time_epoch,
                "live": True,
                "data_freshness": "live",
                "source": "OpenSky Network (Live)",
                "attribution": "Data provided by The OpenSky Network (https://opensky-network.org)",
            }
        elif resp.status_code == 429:
            err_msg = "OpenSky Network rate limit reached (HTTP 429). Throttling requests."
        else:
            err_msg = f"OpenSky Network returned status {resp.status_code}"
    except Exception as exc:
        err_msg = f"Network connection error while fetching flights: {exc}"

    # Return cached data if available
    with _lock:
        if bbox_key in _cache_by_bbox:
            _, cached_data = _cache_by_bbox[bbox_key]
            filtered = _filter_aircraft(cached_data, callsign, icao24, limit)
            return {
                "aircraft": filtered,
                "count": len(filtered),
                "total_in_view": len(cached_data),
                "timestamp": int(time.time()),
                "live": False,
                "data_freshness": "cached",
                "warning": err_msg,
                "source": "OpenSky Network (Cached fallback)",
                "attribution": "Data provided by The OpenSky Network",
            }

    # If completely unavailable, return honest status
    return {
        "aircraft": [],
        "count": 0,
        "total_in_view": 0,
        "timestamp": int(time.time()),
        "live": False,
        "data_freshness": "unavailable",
        "warning": f"Live aircraft data temporarily unavailable ({err_msg})",
        "source": "OpenSky Network",
        "attribution": "Data provided by The OpenSky Network",
    }


def _filter_aircraft(
    aircraft: List[Dict[str, Any]],
    callsign: Optional[str] = None,
    icao24: Optional[str] = None,
    limit: int = 250,
) -> List[Dict[str, Any]]:
    """Filter aircraft by callsign or icao24 and apply limit."""
    res = aircraft
    if icao24:
        target = icao24.strip().lower()
        res = [a for a in res if a["icao24"] == target]
    elif callsign:
        target = callsign.strip().upper()
        res = [a for a in res if target in a["callsign"].upper()]

    return res[:limit]


def get_aircraft_track(
    icao24: str,
    client_id: str = "",
    client_secret: str = "",
) -> Dict[str, Any]:
    """
    Fetch historical track for a specific aircraft from OpenSky Network.
    Returns path waypoints when available.
    """
    clean_icao = icao24.strip().lower()
    headers = {"User-Agent": "JARVIS-Mark-LVII-MapExplorer/1.0"}
    headers.update(_get_auth_header(client_id, client_secret))

    try:
        params = {"icao24": clean_icao, "time": 0}
        resp = requests.get(_OPENSKY_TRACKS_URL, params=params, headers=headers, timeout=8.0)
        if resp.status_code == 200:
            data = resp.json()
            path_pts = data.get("path") or []
            # Each entry in path: [time, latitude, longitude, baro_altitude, true_track, on_ground]
            coords = []
            for pt in path_pts:
                if len(pt) >= 3 and pt[1] is not None and pt[2] is not None:
                    coords.append([round(float(pt[2]), 5), round(float(pt[1]), 5)])

            return {
                "icao24": clean_icao,
                "callsign": (data.get("callsign") or "").strip(),
                "startTime": data.get("startTime"),
                "endTime": data.get("endTime"),
                "coordinates": coords,
                "has_track": len(coords) > 0,
                "source": "OpenSky Network Tracks API",
            }
        else:
            return {
                "icao24": clean_icao,
                "coordinates": [],
                "has_track": False,
                "error": f"OpenSky track returned status {resp.status_code}",
            }
    except Exception as exc:
        return {
            "icao24": clean_icao,
            "coordinates": [],
            "has_track": False,
            "error": str(exc),
        }
