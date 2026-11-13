"""
Geographic Data, Places Explorer, and Intelligence Layers module for JARVIS Map Explorer.
Provides geocoding, reverse geocoding, POI search, curated destination discovery,
USGS live earthquake feed, Open-Meteo weather intelligence, and DMS/Decimal converters.
"""

from __future__ import annotations

import math
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import requests

_lock = threading.Lock()
_geocode_cache: Dict[str, List[Dict[str, Any]]] = {}
_last_nominatim_time = 0.0

# Curated verified geographic destinations catalogue
_CURATED_DESTINATIONS: List[Dict[str, Any]] = [
    # Tourist & Historic Landmarks
    {
        "id": "dest_bekal_fort",
        "name": "Bekal Fort",
        "category": "Historical places",
        "region": "Kerala, India",
        "latitude": 12.3926,
        "longitude": 75.0335,
        "description": "Largest and best-preserved 17th-century historic sea fort in Kerala, built by Shivappa Nayaka.",
        "tags": ["fort", "history", "coastal", "kerala", "landmark"],
    },
    {
        "id": "dest_taj_mahal",
        "name": "Taj Mahal",
        "category": "Famous landmarks",
        "region": "Agra, Uttar Pradesh, India",
        "latitude": 27.1751,
        "longitude": 78.0421,
        "description": "17th-century white marble mausoleum and UNESCO World Heritage site on the Yamuna river.",
        "tags": ["heritage", "unesco", "monument", "marble"],
    },
    {
        "id": "dest_eiffel_tower",
        "name": "Eiffel Tower",
        "category": "Famous landmarks",
        "region": "Paris, France",
        "latitude": 48.8584,
        "longitude": 2.2945,
        "description": "Iconic 330-metre wrought-iron lattice tower on the Champ de Mars, designed by Gustave Eiffel.",
        "tags": ["tower", "paris", "architecture", "monument"],
    },
    {
        "id": "dest_colosseum",
        "name": "Colosseum",
        "category": "Historical places",
        "region": "Rome, Italy",
        "latitude": 41.8902,
        "longitude": 12.4922,
        "description": "Flavian Amphitheatre, the largest ancient amphitheatre ever built, completed in 80 AD.",
        "tags": ["ancient", "rome", "amphitheatre", "unesco"],
    },
    {
        "id": "dest_pyramids_giza",
        "name": "Great Pyramid of Giza",
        "category": "Historical places",
        "region": "Giza, Egypt",
        "latitude": 29.9792,
        "longitude": 31.1342,
        "description": "Oldest and only largely intact monument of the Seven Wonders of the Ancient World.",
        "tags": ["pyramid", "ancient", "egypt", "wonders"],
    },
    # Beaches & Coastal
    {
        "id": "dest_kovalam",
        "name": "Kovalam Beach",
        "category": "Beaches",
        "region": "Thiruvananthapuram, Kerala, India",
        "latitude": 8.4021,
        "longitude": 76.9787,
        "description": "World-famous crescent beach known for its striped lighthouse and Arabian Sea waters.",
        "tags": ["beach", "lighthouse", "kerala", "coast"],
    },
    {
        "id": "dest_marari",
        "name": "Marari Beach",
        "category": "Beaches",
        "region": "Alappuzha, Kerala, India",
        "latitude": 9.6012,
        "longitude": 76.2974,
        "description": "Serene coconut-fringed beach and traditional fishing village on the Malabar coast.",
        "tags": ["beach", "serene", "kerala"],
    },
    {
        "id": "dest_bondi",
        "name": "Bondi Beach",
        "category": "Beaches",
        "region": "Sydney, Australia",
        "latitude": -33.8915,
        "longitude": 151.2767,
        "description": "Crescent of golden sand and one of Australia's most famous surf destinations.",
        "tags": ["beach", "surf", "sydney"],
    },
    # Mountains & Natural Attractions
    {
        "id": "dest_ranipuram",
        "name": "Ranipuram Hills",
        "category": "Mountains",
        "region": "Kasaragod, Kerala, India",
        "latitude": 12.4278,
        "longitude": 75.3524,
        "description": "Scenic Western Ghats hill station known as the 'Ooty of Kerala', with lush shola grasslands.",
        "tags": ["hills", "trekking", "western ghats", "kasaragod"],
    },
    {
        "id": "dest_munnar",
        "name": "Munnar Tea Plantations",
        "category": "Mountains",
        "region": "Idukki, Kerala, India",
        "latitude": 10.0889,
        "longitude": 77.0595,
        "description": "Hill station in the Western Ghats famous for rolling tea estates, mist, and Anamudi peak.",
        "tags": ["tea", "mountains", "hills", "kerala"],
    },
    {
        "id": "dest_everest_base",
        "name": "Mount Everest",
        "category": "Mountains",
        "region": "Solukhumbu, Nepal",
        "latitude": 27.9881,
        "longitude": 86.9250,
        "description": "Earth's highest mountain above sea level, located in the Mahalangur Himal sub-range of the Himalayas.",
        "tags": ["peak", "himalayas", "highest", "snow"],
    },
    {
        "id": "dest_fuji",
        "name": "Mount Fuji",
        "category": "Mountains",
        "region": "Honshu, Japan",
        "latitude": 35.3606,
        "longitude": 138.7274,
        "description": "Japan's highest peak at 3,776m, an active stratovolcano and sacred cultural symbol.",
        "tags": ["volcano", "japan", "peak", "unesco"],
    },
    # National Parks
    {
        "id": "dest_silent_valley",
        "name": "Silent Valley National Park",
        "category": "National parks",
        "region": "Palakkad, Kerala, India",
        "latitude": 11.1353,
        "longitude": 76.4278,
        "description": "Pristine core of the Nilgiri Biosphere Reserve home to endangered lion-tailed macaques.",
        "tags": ["rainforest", "wildlife", "nilgiris", "biodiversity"],
    },
    {
        "id": "dest_yellowstone",
        "name": "Yellowstone National Park",
        "category": "National parks",
        "region": "Wyoming, USA",
        "latitude": 44.4280,
        "longitude": -110.5885,
        "description": "First national park in the world, renowned for wildlife, geysers like Old Faithful, and canyons.",
        "tags": ["geysers", "national park", "wildlife", "usa"],
    },
    # Popular Cities
    {
        "id": "dest_tokyo",
        "name": "Tokyo",
        "category": "Popular cities",
        "region": "Kanto, Japan",
        "latitude": 35.6762,
        "longitude": 139.6503,
        "description": "Metropolis blending ultramodern skyscrapers and historic temples, world's most populous metropolitan area.",
        "tags": ["city", "japan", "metropolis"],
    },
    {
        "id": "dest_new_york",
        "name": "New York City",
        "category": "Popular cities",
        "region": "New York, USA",
        "latitude": 40.7128,
        "longitude": -74.0060,
        "description": "Global center of finance, culture, and architecture, featuring Manhattan, Central Park, and Broadway.",
        "tags": ["city", "manhattan", "usa"],
    },
    {
        "id": "dest_london",
        "name": "London",
        "category": "Popular cities",
        "region": "England, United Kingdom",
        "latitude": 51.5074,
        "longitude": -0.1278,
        "description": "Historic capital on the River Thames, home to Big Ben, Tower Bridge, and the British Museum.",
        "tags": ["city", "uk", "capital"],
    },
    {
        "id": "dest_dubai",
        "name": "Dubai",
        "category": "Popular cities",
        "region": "United Arab Emirates",
        "latitude": 25.2048,
        "longitude": 55.2708,
        "description": "Modern desert metropolis known for luxury shopping, futuristic architecture, and the Burj Khalifa.",
        "tags": ["city", "dubai", "architecture"],
    },
]


def search_locations(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Search for places, cities, addresses, or coordinates.
    Checks:
    1. Direct coordinate regex (DMS or Decimal)
    2. Open-Meteo high-speed global geocoding API
    3. Nominatim (OpenStreetMap) fallback for detailed street addresses
    4. Curated destination matches
    """
    clean_q = query.strip()
    if not clean_q:
        return []

    # Check coordinate input first
    coord_match = parse_coordinates_input(clean_q)
    if coord_match:
        lat, lon = coord_match
        reverse = reverse_geocode(lat, lon)
        name = reverse.get("name") or f"Coordinates ({round(lat, 5)}, {round(lon, 5)})"
        return [{
            "id": f"coord_{round(lat, 4)}_{round(lon, 4)}",
            "name": name,
            "display_name": reverse.get("display_name") or name,
            "latitude": lat,
            "longitude": lon,
            "category": "Coordinates",
            "source": "Coordinate Parser",
        }]

    cache_key = clean_q.lower()
    with _lock:
        if cache_key in _geocode_cache:
            return _geocode_cache[cache_key]

    results: List[Dict[str, Any]] = []

    # Fast Open-Meteo Geocoding
    try:
        url = "https://geocoding-api.open-meteo.com/v1/search"
        params = {"name": clean_q, "count": limit, "language": "en", "format": "json"}
        resp = requests.get(url, params=params, timeout=5.0, headers={"User-Agent": "JARVIS-MapExplorer/1.0"})
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get("results", []):
                country = item.get("country", "")
                admin1 = item.get("admin1", "")
                admin2 = item.get("admin2", "")
                parts = [p for p in (item.get("name"), admin2, admin1, country) if p]
                display_name = ", ".join(parts)

                results.append({
                    "id": f"om_{item.get('id', '')}",
                    "name": item.get("name", clean_q),
                    "display_name": display_name,
                    "latitude": round(float(item.get("latitude", 0.0)), 5),
                    "longitude": round(float(item.get("longitude", 0.0)), 5),
                    "country": country,
                    "region": admin1,
                    "population": item.get("population"),
                    "elevation": item.get("elevation"),
                    "timezone": item.get("timezone"),
                    "category": _feature_code_to_category(item.get("feature_code", "")),
                    "source": "Open-Meteo Geocoding",
                })
    except Exception as exc:
        print(f"[MapExplorer Data] Open-Meteo geocode error: {exc}")

    # If results are sparse, check curated destinations
    q_words = clean_q.lower().split()
    for dest in _CURATED_DESTINATIONS:
        name_lower = dest["name"].lower()
        reg_lower = dest["region"].lower()
        if any(w in name_lower or w in reg_lower or w in dest["tags"] for w in q_words):
            if not any(r["name"].lower() == dest["name"].lower() for r in results):
                results.insert(0, {
                    "id": dest["id"],
                    "name": dest["name"],
                    "display_name": f"{dest['name']}, {dest['region']}",
                    "latitude": dest["latitude"],
                    "longitude": dest["longitude"],
                    "category": dest["category"],
                    "description": dest["description"],
                    "source": "JARVIS Curated Geographic Database",
                })

    with _lock:
        _geocode_cache[cache_key] = results[:limit]

    return results[:limit]


def search_places_nearby(
    category: str = "",
    latitude: float = 0.0,
    longitude: float = 0.0,
    radius_km: float = 25.0,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    Search for points of interest near coordinates (hotels, restaurants, hospitals,
    airports, railway stations, fuel, attractions) using OpenStreetMap Overpass or Photon.
    """
    cat_lower = category.lower().strip()
    tag_filter = _map_category_to_osm_tag(cat_lower)

    # Overpass bounding box query: (lat - d, lon - d, lat + d, lon + d)
    lat_delta = radius_km / 111.0
    lon_delta = radius_km / (111.0 * max(0.1, math.cos(math.radians(latitude))))
    s, w = latitude - lat_delta, longitude - lon_delta
    n, e = latitude + lat_delta, longitude + lon_delta

    query_body = f"""
    [out:json][timeout:10];
    (
      node{tag_filter}({s},{w},{n},{e});
      way{tag_filter}({s},{w},{n},{e});
    );
    out center {limit};
    """

    overpass_urls = [
        "https://overpass-api.de/api/interpreter",
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    ]

    for op_url in overpass_urls:
        try:
            resp = requests.post(
                op_url,
                data={"data": query_body},
                timeout=9.0,
                headers={"User-Agent": "JARVIS-Mark-LVII-MapExplorer/1.0"},
            )
            if resp.status_code == 200:
                elements = resp.json().get("elements", [])
                places = []
                for el in elements:
                    tags = el.get("tags", {})
                    name = tags.get("name")
                    if not name:
                        continue
                    p_lat = el.get("lat") or el.get("center", {}).get("lat")
                    p_lon = el.get("lon") or el.get("center", {}).get("lon")
                    if p_lat is None or p_lon is None:
                        continue

                    dist_km = round(_haversine_distance(latitude, longitude, p_lat, p_lon) / 1000.0, 2)
                    places.append({
                        "id": f"osm_{el.get('type')}_{el.get('id')}",
                        "name": name,
                        "category": tags.get("tourism") or tags.get("amenity") or tags.get("aeroway") or tags.get("railway") or category,
                        "latitude": round(float(p_lat), 5),
                        "longitude": round(float(p_lon), 5),
                        "distance_km": dist_km,
                        "distance_miles": round(dist_km * 0.621371, 2),
                        "address": tags.get("addr:street") or tags.get("addr:full") or tags.get("addr:city") or "",
                        "phone": tags.get("phone") or tags.get("contact:phone") or "",
                        "website": tags.get("website") or tags.get("contact:website") or "",
                        "source": "OpenStreetMap Overpass API",
                    })

                places.sort(key=lambda x: x["distance_km"])
                return places[:limit]
        except Exception as exc:
            print(f"[MapExplorer Data] Overpass error ({op_url}): {exc}")

    # Fallback: check curated destinations within radius
    fallback_places = []
    for d in _CURATED_DESTINATIONS:
        dist_km = round(_haversine_distance(latitude, longitude, d["latitude"], d["longitude"]) / 1000.0, 2)
        if dist_km <= radius_km * 2:
            fallback_places.append({
                "id": d["id"],
                "name": d["name"],
                "category": d["category"],
                "latitude": d["latitude"],
                "longitude": d["longitude"],
                "distance_km": dist_km,
                "distance_miles": round(dist_km * 0.621371, 2),
                "address": d["region"],
                "source": "JARVIS Curated Geographic Database",
            })

    fallback_places.sort(key=lambda x: x["distance_km"])
    return fallback_places[:limit]


def get_curated_destinations(category: str = "") -> List[Dict[str, Any]]:
    """Retrieve verified destinations filtered optionally by category."""
    cat_clean = category.lower().strip()
    if not cat_clean:
        return list(_CURATED_DESTINATIONS)
    return [d for d in _CURATED_DESTINATIONS if cat_clean in d["category"].lower()]


def reverse_geocode(latitude: float, longitude: float) -> Dict[str, Any]:
    """Reverse geocode latitude and longitude into address."""
    global _last_nominatim_time

    # Throttle Nominatim to 1 request per second
    with _lock:
        now = time.monotonic()
        elapsed = now - _last_nominatim_time
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        _last_nominatim_time = time.monotonic()

    url = "https://nominatim.openstreetmap.org/reverse"
    params = {"lat": latitude, "lon": longitude, "format": "jsonv2", "zoom": 14}
    headers = {"User-Agent": "JARVIS-Mark-LVII-MapExplorer/1.0 (contact: local-desktop@jarvis.internal)"}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=6.0)
        if resp.status_code == 200:
            data = resp.json()
            addr = data.get("address", {})
            name = (
                addr.get("city")
                or addr.get("town")
                or addr.get("village")
                or addr.get("suburb")
                or addr.get("county")
                or data.get("name")
                or "Unknown Location"
            )
            return {
                "name": name,
                "display_name": data.get("display_name", ""),
                "country": addr.get("country", ""),
                "state": addr.get("state", ""),
                "latitude": latitude,
                "longitude": longitude,
                "source": "OpenStreetMap Nominatim",
            }
    except Exception as exc:
        print(f"[MapExplorer Data] Reverse geocode error: {exc}")

    return {
        "name": f"Location ({round(latitude, 4)}, {round(longitude, 4)})",
        "display_name": f"Lat: {latitude}, Lon: {longitude}",
        "latitude": latitude,
        "longitude": longitude,
        "source": "Fallback Coordinates",
    }


def get_earthquake_layer(feed_type: str = "2.5_day") -> Dict[str, Any]:
    """Fetch live USGS earthquakes GeoJSON feed."""
    url = f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{feed_type}.geojson"
    try:
        resp = requests.get(url, timeout=7.0, headers={"User-Agent": "JARVIS-MapExplorer/1.0"})
        if resp.status_code == 200:
            data = resp.json()
            return {
                "type": "FeatureCollection",
                "count": len(data.get("features", [])),
                "features": data.get("features", []),
                "metadata": data.get("metadata", {}),
                "source": "USGS Earthquake Hazards Program (Live)",
                "live": True,
            }
    except Exception as exc:
        print(f"[MapExplorer Data] USGS Earthquakes fetch error: {exc}")

    return {
        "type": "FeatureCollection",
        "count": 0,
        "features": [],
        "source": "USGS (Unavailable)",
        "live": False,
    }


def get_weather_intelligence(latitude: float, longitude: float) -> Dict[str, Any]:
    """Fetch live weather metrics from Open-Meteo."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,cloud_cover,wind_speed_10m,wind_direction_10m",
        "timezone": "auto",
    }
    try:
        resp = requests.get(url, params=params, timeout=6.0, headers={"User-Agent": "JARVIS-MapExplorer/1.0"})
        if resp.status_code == 200:
            data = resp.json()
            curr = data.get("current", {})
            w_code = curr.get("weather_code", 0)
            condition = _wmo_weather_code_to_text(w_code)
            return {
                "latitude": latitude,
                "longitude": longitude,
                "temperature_c": curr.get("temperature_2m"),
                "temperature_f": round(curr.get("temperature_2m", 0.0) * 9.0 / 5.0 + 32.0, 1) if curr.get("temperature_2m") is not None else None,
                "humidity": curr.get("relative_humidity_2m"),
                "apparent_temp_c": curr.get("apparent_temperature"),
                "precipitation_mm": curr.get("precipitation"),
                "cloud_cover_percent": curr.get("cloud_cover"),
                "wind_speed_kmh": curr.get("wind_speed_10m"),
                "wind_direction_deg": curr.get("wind_direction_10m"),
                "condition": condition,
                "weather_code": w_code,
                "source": "Open-Meteo Weather API",
                "live": True,
            }
    except Exception as exc:
        print(f"[MapExplorer Data] Weather fetch error: {exc}")

    return {"live": False, "source": "Open-Meteo (Unavailable)"}


def parse_coordinates_input(text: str) -> Optional[Tuple[float, float]]:
    """
    Parse coordinates in decimal degrees (e.g. '12.308, 75.106')
    or DMS (e.g. '12° 18\' 29" N, 75° 6\' 22" E').
    Returns (lat, lon) or None.
    """
    clean = text.strip()

    # Pattern 1: Decimal degrees: "12.30814, 75.10632" or "12.30814 75.10632"
    dec_match = re.search(r"^[-+]?([0-8]?\d(?:\.\d+)?|90(?:\.0+)?)[,\s]+[-+]?(180(?:\.0+)?|(?:1[0-7]\d|\d{1,2})(?:\.\d+)?)$", clean)
    if dec_match:
        try:
            parts = [float(p) for p in re.split(r"[,\s]+", clean) if p]
            if len(parts) >= 2:
                lat, lon = parts[0], parts[1]
                if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                    return lat, lon
        except ValueError:
            pass

    # Pattern 2: DMS format
    dms_regex = re.compile(
        r"""(\d+)[°\s]+(\d+)['\s]+(?:(\d+(?:\.\d+)?)["]?\s*)?([NSEW])"""
        r"""[,\s]+"""
        r"""(\d+)[°\s]+(\d+)['\s]+(?:(\d+(?:\.\d+)?)["]?\s*)?([NSEW])""",
        re.IGNORECASE | re.VERBOSE,
    )
    match = dms_regex.search(clean)
    if match:
        deg1, min1, sec1, dir1, deg2, min2, sec2, dir2 = match.groups()
        val1 = float(deg1) + float(min1) / 60.0 + (float(sec1 or 0) / 3600.0)
        val2 = float(deg2) + float(min2) / 60.0 + (float(sec2 or 0) / 3600.0)

        lat, lon = None, None
        if dir1.upper() in ("N", "S"):
            lat = -val1 if dir1.upper() == "S" else val1
        elif dir1.upper() in ("E", "W"):
            lon = -val1 if dir1.upper() == "W" else val1

        if dir2.upper() in ("N", "S"):
            lat = -val2 if dir2.upper() == "S" else val2
        elif dir2.upper() in ("E", "W"):
            lon = -val2 if dir2.upper() == "W" else val2

        if lat is not None and lon is not None:
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                return round(lat, 5), round(lon, 5)

    return None


def decimal_to_dms(lat: float, lon: float) -> Dict[str, str]:
    """Convert decimal latitude and longitude to Degrees Minutes Seconds (DMS) string."""
    def _convert(val: float, is_lat: bool) -> str:
        direction = ("N" if val >= 0 else "S") if is_lat else ("E" if val >= 0 else "W")
        abs_v = abs(val)
        d = int(abs_v)
        rem = (abs_v - d) * 60.0
        m = int(rem)
        s = round((rem - m) * 60.0, 1)
        return f"{d}° {m:02d}' {s:04.1f}\" {direction}"

    return {
        "lat_dms": _convert(lat, True),
        "lon_dms": _convert(lon, False),
        "full_dms": f"{_convert(lat, True)}, {_convert(lon, False)}",
    }


def _map_category_to_osm_tag(category: str) -> str:
    """Map user category to OSM Overpass tag filter."""
    if "hotel" in category or "stay" in category:
        return '["tourism"="hotel"]'
    if "restaurant" in category or "food" in category or "cafe" in category:
        return '["amenity"~"restaurant|cafe|fast_food"]'
    if "hospital" in category or "doctor" in category or "clinic" in category:
        return '["amenity"~"hospital|clinic|pharmacy"]'
    if "airport" in category:
        return '["aeroway"~"aerodrome|terminal"]'
    if "railway" in category or "train" in category:
        return '["railway"~"station|halt"]'
    if "fuel" in category or "gas" in category:
        return '["amenity"="fuel"]'
    if "beach" in category:
        return '["natural"="beach"]'
    if "museum" in category:
        return '["tourism"="museum"]'
    if "attraction" in category or "tourist" in category:
        return '["tourism"~"attraction|theme_park|viewpoint"]'

    return '["tourism"~"attraction|hotel|viewpoint"]'


def _feature_code_to_category(code: str) -> str:
    """Translate Open-Meteo GeoNames feature code to friendly category."""
    if code.startswith("PPL"):
        return "City / Settlement"
    if code == "AIRP":
        return "Airport"
    if code.startswith("H"):
        return "Water / Coast"
    if code.startswith("T"):
        return "Mountain / Hill"
    if code.startswith("L"):
        return "Park / Reserve"
    if code.startswith("S"):
        return "Building / Landmark"
    return "Place"


def _wmo_weather_code_to_text(code: int) -> str:
    """Translate WMO weather code to readable string."""
    codes = {
        0: "Clear sky",
        1: "Mainly clear",
        2: "Partly cloudy",
        3: "Overcast",
        45: "Fog",
        48: "Depositing rime fog",
        51: "Light drizzle",
        53: "Moderate drizzle",
        55: "Dense drizzle",
        61: "Slight rain",
        63: "Moderate rain",
        65: "Heavy rain",
        71: "Slight snowfall",
        73: "Moderate snowfall",
        75: "Heavy snowfall",
        80: "Slight rain showers",
        81: "Moderate rain showers",
        82: "Violent rain showers",
        95: "Thunderstorm",
        96: "Thunderstorm with slight hail",
        99: "Thunderstorm with heavy hail",
    }
    return codes.get(code, "Unknown conditions")


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two lat/lon points."""
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2)
    return r * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
