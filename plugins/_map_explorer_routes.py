"""
Routing and Navigation module for JARVIS Map Explorer.
Calculates road-accurate routes for driving, walking, and cycling using OSRM,
OpenRouteService, or GraphHopper, with turn-by-turn guidance and elevation profile.
"""

from __future__ import annotations

import math
import threading
from typing import Any, Dict, List, Optional, Tuple
import requests

_lock = threading.Lock()
_route_cache: Dict[str, Dict[str, Any]] = {}

_DEFAULT_OSRM_URL = "https://router.project-osrm.org"


def calculate_route(
    coordinates: List[Tuple[float, float]],
    travel_mode: str = "driving",
    provider: str = "osrm",
    api_key: str = "",
    custom_osrm_url: str = "",
    include_elevation: bool = True,
) -> Dict[str, Any]:
    """
    Calculate an actual road-following route between 2 or more coordinates.
    coordinates: List of (longitude, latitude) pairs in WGS84 format.
    travel_mode: 'driving', 'walking', or 'cycling'.
    provider: 'osrm', 'openrouteservice', or 'graphhopper'.
    """
    if len(coordinates) < 2:
        return {
            "success": False,
            "error": "At least two coordinates (origin and destination) are required to plan a route.",
        }

    # Validate coordinate values
    for lon, lat in coordinates:
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            return {
                "success": False,
                "error": f"Invalid coordinate: longitude {lon}, latitude {lat} outside geographic boundaries.",
            }

    mode_clean = travel_mode.lower().strip()
    if mode_clean not in ("driving", "walking", "cycling"):
        mode_clean = "driving"

    # Cache key
    coord_key = ";".join(f"{round(c[0], 5)},{round(c[1], 5)}" for c in coordinates)
    cache_key = f"{provider}:{mode_clean}:{coord_key}"
    with _lock:
        if cache_key in _route_cache:
            return dict(_route_cache[cache_key])

    # Route with selected provider
    if provider == "openrouteservice":
        result = _route_openrouteservice(coordinates, mode_clean, api_key)
    elif provider == "graphhopper":
        result = _route_graphhopper(coordinates, mode_clean, api_key)
    else:  # osrm default
        base_url = custom_osrm_url.strip() if custom_osrm_url else _DEFAULT_OSRM_URL
        result = _route_osrm(coordinates, mode_clean, base_url)

    # Attach elevation profile if successful
    if result.get("success") and include_elevation:
        coords = result.get("geometry", {}).get("coordinates", [])
        if coords:
            elevations = _fetch_elevation_profile(coords)
            result["elevation_profile"] = elevations

    if result.get("success"):
        with _lock:
            _route_cache[cache_key] = result

    return result


def _route_osrm(
    coordinates: List[Tuple[float, float]],
    mode: str,
    base_url: str,
) -> Dict[str, Any]:
    """Calculate route via OSRM."""
    osrm_profile = "driving"
    if mode == "walking":
        osrm_profile = "foot"
    elif mode == "cycling":
        osrm_profile = "bike"

    # Coordinates format: lon1,lat1;lon2,lat2;...
    coords_str = ";".join(f"{c[0]},{c[1]}" for c in coordinates)
    url = f"{base_url.rstrip('/')}/route/v1/{osrm_profile}/{coords_str}"
    params = {
        "overview": "full",
        "geometries": "geojson",
        "steps": "true",
        "annotations": "false",
    }

    try:
        resp = requests.get(url, params=params, timeout=12.0, headers={"User-Agent": "JARVIS-MapExplorer/1.0"})
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") != "Ok" or not data.get("routes"):
                return {
                    "success": False,
                    "error": f"OSRM router could not find a traversable road path ({data.get('code', 'No route')}).",
                    "provider": "OSRM",
                }

            best_route = data["routes"][0]
            distance_m = float(best_route.get("distance", 0.0))
            duration_s = float(best_route.get("duration", 0.0))

            # Turn-by-turn guidance extraction
            steps_list: List[Dict[str, Any]] = []
            legs = best_route.get("legs", [])
            for leg_idx, leg in enumerate(legs):
                for step in leg.get("steps", []):
                    maneuver = step.get("maneuver", {})
                    m_type = maneuver.get("type", "continue")
                    m_modifier = maneuver.get("modifier", "")
                    name = step.get("name") or "unnamed road"
                    instruction = _format_maneuver_instruction(m_type, m_modifier, name)

                    steps_list.append({
                        "instruction": instruction,
                        "type": m_type,
                        "modifier": m_modifier,
                        "name": name,
                        "distance_m": round(float(step.get("distance", 0.0))),
                        "distance_km": round(float(step.get("distance", 0.0)) / 1000.0, 2),
                        "distance_miles": round(float(step.get("distance", 0.0)) * 0.000621371, 2),
                        "duration_s": round(float(step.get("duration", 0.0))),
                        "location": maneuver.get("location", [0, 0]),
                    })

            # Calculate bounds
            all_coords = best_route.get("geometry", {}).get("coordinates", [])
            bounds = _compute_bbox(all_coords)

            return {
                "success": True,
                "provider": "OSRM (Open Source Routing Machine)",
                "provider_attribution": "Routing data © OpenStreetMap contributors, OSRM",
                "travel_mode": mode,
                "distance_meters": round(distance_m, 1),
                "distance_km": round(distance_m / 1000.0, 2),
                "distance_miles": round(distance_m * 0.000621371, 2),
                "duration_seconds": round(duration_s),
                "duration_text": _format_duration(duration_s),
                "geometry": best_route.get("geometry"),
                "turn_by_turn": steps_list,
                "bounds": bounds,
                "waypoints": [
                    {"index": i, "location": [c[0], c[1]]}
                    for i, c in enumerate(coordinates)
                ],
            }
        else:
            return {
                "success": False,
                "error": f"OSRM routing request failed with HTTP {resp.status_code}.",
                "provider": "OSRM",
            }
    except Exception as exc:
        return {
            "success": False,
            "error": f"Could not reach routing server ({exc}). Check internet connection or self-hosted OSRM settings.",
            "provider": "OSRM",
        }


def _route_openrouteservice(
    coordinates: List[Tuple[float, float]],
    mode: str,
    api_key: str,
) -> Dict[str, Any]:
    """Calculate route via OpenRouteService API."""
    if not api_key:
        return {
            "success": False,
            "error": "OpenRouteService API key not configured. Please add your API key in Plugin Settings.",
            "provider": "OpenRouteService",
        }

    ors_profile = "driving-car"
    if mode == "walking":
        ors_profile = "foot-walking"
    elif mode == "cycling":
        ors_profile = "cycling-regular"

    url = f"https://api.openrouteservice.org/v2/directions/{ors_profile}/geojson"
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
        "User-Agent": "JARVIS-MapExplorer/1.0",
    }
    body = {"coordinates": coordinates}

    try:
        resp = requests.post(url, json=body, headers=headers, timeout=12.0)
        if resp.status_code == 200:
            data = resp.json()
            features = data.get("features", [])
            if not features:
                return {"success": False, "error": "No route returned from OpenRouteService."}

            feat = features[0]
            props = feat.get("properties", {}).get("summary", {})
            distance_m = float(props.get("distance", 0.0))
            duration_s = float(props.get("duration", 0.0))
            geom = feat.get("geometry", {})
            bounds = _compute_bbox(geom.get("coordinates", []))

            # Turn-by-turn segments
            steps_list: List[Dict[str, Any]] = []
            segments = feat.get("properties", {}).get("segments", [])
            for seg in segments:
                for step in seg.get("steps", []):
                    steps_list.append({
                        "instruction": step.get("instruction", ""),
                        "name": step.get("name", "road"),
                        "distance_m": round(float(step.get("distance", 0.0))),
                        "distance_km": round(float(step.get("distance", 0.0)) / 1000.0, 2),
                        "distance_miles": round(float(step.get("distance", 0.0)) * 0.000621371, 2),
                        "duration_s": round(float(step.get("duration", 0.0))),
                        "type": str(step.get("type", 0)),
                    })

            return {
                "success": True,
                "provider": "OpenRouteService",
                "provider_attribution": "© openrouteservice.org by HeiGIT, OpenStreetMap contributors",
                "travel_mode": mode,
                "distance_meters": round(distance_m, 1),
                "distance_km": round(distance_m / 1000.0, 2),
                "distance_miles": round(distance_m * 0.000621371, 2),
                "duration_seconds": round(duration_s),
                "duration_text": _format_duration(duration_s),
                "geometry": geom,
                "turn_by_turn": steps_list,
                "bounds": bounds,
            }
        else:
            return {
                "success": False,
                "error": f"OpenRouteService returned HTTP {resp.status_code}: {resp.text[:200]}",
                "provider": "OpenRouteService",
            }
    except Exception as exc:
        return {"success": False, "error": f"OpenRouteService error: {exc}", "provider": "OpenRouteService"}


def _route_graphhopper(
    coordinates: List[Tuple[float, float]],
    mode: str,
    api_key: str,
) -> Dict[str, Any]:
    """Calculate route via GraphHopper API."""
    if not api_key:
        return {
            "success": False,
            "error": "GraphHopper API key not configured. Please add your API key in Plugin Settings.",
            "provider": "GraphHopper",
        }

    gh_vehicle = "car"
    if mode == "walking":
        gh_vehicle = "foot"
    elif mode == "cycling":
        gh_vehicle = "bike"

    url = "https://graphhopper.com/api/1/route"
    # Format point params: point=lat,lon
    params: List[Tuple[str, str]] = [("key", api_key), ("vehicle", gh_vehicle), ("points_encoded", "false")]
    for lon, lat in coordinates:
        params.append(("point", f"{lat},{lon}"))

    try:
        resp = requests.get(url, params=params, timeout=12.0)
        if resp.status_code == 200:
            data = resp.json()
            paths = data.get("paths", [])
            if not paths:
                return {"success": False, "error": "No route returned by GraphHopper."}

            path = paths[0]
            distance_m = float(path.get("distance", 0.0))
            duration_ms = float(path.get("time", 0.0))
            duration_s = duration_ms / 1000.0
            points_geom = path.get("points", {})
            bounds = _compute_bbox(points_geom.get("coordinates", []))

            steps_list: List[Dict[str, Any]] = []
            for instr in path.get("instructions", []):
                dist_m = float(instr.get("distance", 0.0))
                steps_list.append({
                    "instruction": instr.get("text", ""),
                    "distance_m": round(dist_m),
                    "distance_km": round(dist_m / 1000.0, 2),
                    "distance_miles": round(dist_m * 0.000621371, 2),
                    "duration_s": round(float(instr.get("time", 0.0)) / 1000.0),
                    "name": instr.get("street_name", ""),
                })

            return {
                "success": True,
                "provider": "GraphHopper",
                "provider_attribution": "© GraphHopper, OpenStreetMap contributors",
                "travel_mode": mode,
                "distance_meters": round(distance_m, 1),
                "distance_km": round(distance_m / 1000.0, 2),
                "distance_miles": round(distance_m * 0.000621371, 2),
                "duration_seconds": round(duration_s),
                "duration_text": _format_duration(duration_s),
                "geometry": points_geom,
                "turn_by_turn": steps_list,
                "bounds": bounds,
            }
        else:
            return {
                "success": False,
                "error": f"GraphHopper returned HTTP {resp.status_code}: {resp.text[:200]}",
                "provider": "GraphHopper",
            }
    except Exception as exc:
        return {"success": False, "error": f"GraphHopper error: {exc}", "provider": "GraphHopper"}


def _fetch_elevation_profile(coordinates: List[List[float]], max_samples: int = 30) -> List[Dict[str, Any]]:
    """Sample coordinates and fetch elevation from Open-Meteo elevation API."""
    if not coordinates:
        return []

    # Sample coordinates evenly
    total = len(coordinates)
    if total <= max_samples:
        sampled = coordinates
    else:
        step = total / float(max_samples)
        sampled = [coordinates[int(i * step)] for i in range(max_samples)]
        if coordinates[-1] not in sampled:
            sampled.append(coordinates[-1])

    lats = [str(c[1]) for c in sampled]
    lons = [str(c[0]) for c in sampled]

    try:
        url = "https://api.open-meteo.com/v1/elevation"
        resp = requests.get(url, params={"latitude": ",".join(lats), "longitude": ",".join(lons)}, timeout=6.0)
        if resp.status_code == 200:
            elevs = resp.json().get("elevation", [])
            profile = []
            cum_dist_m = 0.0
            for idx, c in enumerate(sampled):
                if idx > 0:
                    prev = sampled[idx - 1]
                    cum_dist_m += _haversine_distance(prev[1], prev[0], c[1], c[0])
                elev_m = elevs[idx] if idx < len(elevs) else 0.0
                profile.append({
                    "distance_km": round(cum_dist_m / 1000.0, 2),
                    "elevation_meters": elev_m,
                    "elevation_feet": round(elev_m * 3.28084, 1) if elev_m is not None else 0.0,
                    "latitude": c[1],
                    "longitude": c[0],
                })
            return profile
    except Exception as exc:
        print(f"[MapExplorer Routes] Elevation profile fetch error: {exc}")

    return []


def _compute_bbox(coordinates: List[List[float]]) -> List[float]:
    """Compute [min_lon, min_lat, max_lon, max_lat] bounding box."""
    if not coordinates:
        return [-180, -90, 180, 90]
    min_lon = min(c[0] for c in coordinates)
    max_lon = max(c[0] for c in coordinates)
    min_lat = min(c[1] for c in coordinates)
    max_lat = max(c[1] for c in coordinates)
    return [round(min_lon, 5), round(min_lat, 5), round(max_lon, 5), round(max_lat, 5)]


def _format_maneuver_instruction(m_type: str, modifier: str, name: str) -> str:
    """Produce clean human-readable turn instruction."""
    m_type = m_type.lower()
    modifier = modifier.lower().replace("_", " ")

    if m_type == "depart":
        return f"Head {modifier} on {name}" if modifier else f"Depart on {name}"
    if m_type == "arrive":
        return "You have arrived at your destination"
    if m_type == "turn":
        return f"Turn {modifier} onto {name}" if modifier else f"Turn onto {name}"
    if m_type == "roundabout":
        return f"Enter roundabout and take exit onto {name}"
    if m_type == "merge":
        return f"Merge {modifier} onto {name}" if modifier else f"Merge onto {name}"
    if m_type == "fork":
        return f"Keep {modifier} at the fork onto {name}" if modifier else f"Take fork onto {name}"
    if m_type == "end of road":
        return f"At end of road, turn {modifier} onto {name}"

    return f"Continue on {name}"


def _format_duration(seconds: float) -> str:
    """Format duration in hours and minutes."""
    mins = int(round(seconds / 60.0))
    if mins < 60:
        return f"{mins} min"
    hrs = mins // 60
    rem_mins = mins % 60
    return f"{hrs} hr {rem_mins} min" if rem_mins else f"{hrs} hr"


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in meters between two coordinates."""
    r = 6371000.0  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c
