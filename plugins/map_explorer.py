"""
JARVIS Map Explorer & 3D Earth Intelligence Plugin.
Provides full interactive 2D maps, 3D Earth globe, road-accurate turn-by-turn navigation,
live flight tracking with OpenSky Network, ISS telemetry & orbital tracking,
and global destination discovery for MARK LVII.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from plugins._map_explorer_data import (
    decimal_to_dms,
    get_curated_destinations,
    reverse_geocode,
    search_locations,
    search_places_nearby,
)
from plugins._map_explorer_flights import query_flights
from plugins._map_explorer_iss import get_iss_telemetry
from plugins._map_explorer_routes import calculate_route
from plugins._map_explorer_server import MapExplorerServerManager

PLUGIN = {
    "name": "map_explorer",
    "description": (
        "Comprehensive interactive 2D map, 3D Earth globe, turn-by-turn navigation, "
        "live flight radar, and ISS tracking system for JARVIS MARK LVII. "
        "Use this tool whenever the user asks to open the map, view the 3D globe or Earth, "
        "switch to satellite view, search for a location or city, calculate driving/walking/cycling routes, "
        "track flights and aircraft, find the ISS position and orbit, discover tourist attractions, "
        "find nearby hotels, restaurants, hospitals, or airports, or close the map."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "Action to perform: 'open', 'show_globe', 'show_map', 'set_layer', "
                    "'search', 'navigate', 'track_flight', 'track_iss', 'find_places', "
                    "'discover_destinations', 'clear_route', 'close', or 'status'."
                ),
            },
            "location": {
                "type": "STRING",
                "description": "Target place, city, landmark, or coordinates to find on the map.",
            },
            "origin": {
                "type": "STRING",
                "description": "Starting location for route planning or navigation.",
            },
            "destination": {
                "type": "STRING",
                "description": "Destination location for route planning or navigation.",
            },
            "waypoints": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Optional intermediate stops or waypoints along the route.",
            },
            "travel_mode": {
                "type": "STRING",
                "description": "Travel mode: 'driving', 'walking', or 'cycling'.",
            },
            "map_view": {
                "type": "STRING",
                "description": "Map view projection: '2d', '3d', 'globe', or 'mercator'.",
            },
            "map_layer": {
                "type": "STRING",
                "description": "Map layer style: 'streets', 'dark', 'light', 'topo', 'satellite', 'satellite_labels', or 'terrain'.",
            },
            "zoom": {
                "type": "NUMBER",
                "description": "Map zoom level (1 to 20).",
            },
            "search_query": {
                "type": "STRING",
                "description": "General search query for places, landmarks, or points of interest.",
            },
            "aircraft_callsign": {
                "type": "STRING",
                "description": "Callsign of an aircraft to track on the flight radar (e.g. 'AXB2205').",
            },
            "aircraft_icao24": {
                "type": "STRING",
                "description": "ICAO24 24-bit hex transponder address of an aircraft.",
            },
            "latitude": {
                "type": "NUMBER",
                "description": "Target geographic latitude (-90 to +90).",
            },
            "longitude": {
                "type": "NUMBER",
                "description": "Target geographic longitude (-180 to +180).",
            },
        },
        "required": [],
    },
    "behavior": "NON_BLOCKING",
    "tier": "core",
}

PLUGIN_SETTINGS = {
    "namespace": "map_explorer",
    "title": "Map Explorer & 3D Globe",
    "fields": [
        {
            "key": "map_tile_provider",
            "label": "Base Map Style",
            "type": "choice",
            "options": [
                "CartoDB Voyager (Street)",
                "CartoDB Dark Matter",
                "CartoDB Positron",
                "ESRI World Imagery (Satellite)",
                "ESRI Satellite + Labels",
                "OpenTopoMap (Topographic)",
                "Custom Vector Style URL",
            ],
            "default": "CartoDB Voyager (Street)",
        },
        {
            "key": "custom_style_url",
            "label": "Custom Map Style URL",
            "type": "text",
            "default": "",
            "placeholder": "https://api.maptiler.com/maps/.../style.json?key=...",
        },
        {
            "key": "map_api_key",
            "label": "Map Provider API Key (Optional)",
            "type": "password",
            "default": "",
            "placeholder": "Mapbox / MapTiler API Key",
        },
        {
            "key": "routing_provider",
            "label": "Routing Provider",
            "type": "choice",
            "options": ["OSRM Public", "OpenRouteService", "GraphHopper", "Self-Hosted OSRM"],
            "default": "OSRM Public",
        },
        {
            "key": "routing_api_key",
            "label": "Routing API Key (Optional)",
            "type": "password",
            "default": "",
            "placeholder": "OpenRouteService or GraphHopper Key",
        },
        {
            "key": "custom_osrm_url",
            "label": "Custom OSRM URL (Optional)",
            "type": "text",
            "default": "",
            "placeholder": "http://localhost:5000",
        },
        {
            "key": "opensky_client_id",
            "label": "OpenSky OAuth2 Client ID",
            "type": "text",
            "default": "",
            "placeholder": "OpenSky Client ID",
        },
        {
            "key": "opensky_client_secret",
            "label": "OpenSky OAuth2 Secret",
            "type": "password",
            "default": "",
            "placeholder": "OpenSky Client Secret",
        },
        {
            "key": "places_provider",
            "label": "Places Search Provider",
            "type": "choice",
            "options": ["Open-Meteo & Overpass (Free)", "Nominatim OSM", "Photon"],
            "default": "Open-Meteo & Overpass (Free)",
        },
        {
            "key": "measurement_units",
            "label": "Measurement Units",
            "type": "choice",
            "options": ["Metric (km, m, °C)", "Imperial (mi, ft, °F)"],
            "default": "Metric (km, m, °C)",
        },
        {
            "key": "low_performance_mode",
            "label": "Low Performance Mode (Low GPU)",
            "type": "toggle",
            "default": False,
        },
    ],
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """
    Execute voice or programmatic commands for Map Explorer.
    Dispatches to local web server, pushes state via SSE, updates HUD, and returns spoken response.
    """
    try:
        manager = MapExplorerServerManager.get_instance()
        action = (parameters.get("action") or "").lower().strip()
        loc = parameters.get("location") or parameters.get("search_query") or ""
        origin = parameters.get("origin") or ""
        destination = parameters.get("destination") or ""
        waypoints = parameters.get("waypoints") or []
        travel_mode = (parameters.get("travel_mode") or "driving").lower()
        map_view = (parameters.get("map_view") or "").lower()
        map_layer = (parameters.get("map_layer") or "").lower()
        callsign = parameters.get("aircraft_callsign") or ""
        icao24 = parameters.get("aircraft_icao24") or ""
        lat = parameters.get("latitude")
        lon = parameters.get("longitude")

        # Normalize implicit action intent
        if not action:
            if origin and destination:
                action = "navigate"
            elif callsign or icao24:
                action = "track_flight"
            elif map_view in ("globe", "3d", "sphere"):
                action = "show_globe"
            elif map_view in ("2d", "map", "mercator"):
                action = "show_map"
            elif map_layer:
                action = "set_layer"
            elif loc:
                action = "search"
            else:
                action = "open"

        # Action: Close map explorer
        if action in ("close", "exit", "shutdown"):
            manager.broadcast_command("close_session")
            if player and hasattr(player, "close_hud_view"):
                player.close_hud_view("map_explorer")
            _log_and_hud(player, "JARVIS MAP EXPLORER", "Map Explorer session deactivated.", "Map Explorer deactivated.")
            return "Sir, I have closed the active map session and restored the main HUD."

        # Ensure server is running and display inside the main HUD
        map_url = manager.start()
        if player and hasattr(player, "show_hud_view"):
            player.show_hud_view(
                "map_explorer",
                "MAP EXPLORER",
                map_url,
                content_type="url",
                replace=True,
            )

        # Action: Show 3D Globe
        if action in ("show_globe", "globe", "3d_earth"):
            manager.broadcast_command("set_projection", {"projection": "globe"})
            _log_and_hud(player, "JARVIS 3D GLOBE", "Projection: 3D Spherical Earth\nAtmospheric glow: Active\nSolar Terminator: Synchronized", "3D Earth Globe activated.")
            return "Switching to the 3D interactive Earth globe, sir."

        # Action: Show 2D Map
        if action in ("show_map", "map", "2d_map", "normal_map"):
            manager.broadcast_command("set_projection", {"projection": "mercator"})
            _log_and_hud(player, "JARVIS 2D MAP", "Projection: Flat Mercator Map\nView: Standard cartographic orientation", "2D Map view activated.")
            return "Switching back to standard 2D cartographic map view, sir."

        # Action: Set layer / style
        if action in ("set_layer", "layer", "style"):
            layer_name = map_layer or "streets"
            if "sat" in layer_name or "satellite" in layer_name:
                layer_name = "satellite_labels" if "label" in layer_name else "satellite"
            elif "dark" in layer_name:
                layer_name = "dark"
            elif "light" in layer_name:
                layer_name = "light"
            elif "topo" in layer_name or "terrain" in layer_name:
                layer_name = "topo"
            else:
                layer_name = "streets"

            manager.broadcast_command("set_layer", {"layer": layer_name})
            _log_and_hud(player, "JARVIS MAP STYLE", f"Active Layer: {layer_name.title()}\nStatus: Online", f"Map layer changed to {layer_name}.")
            return f"Map layer switched to {layer_name.replace('_', ' ')}, sir."

        # Action: Navigation / Routing
        if action in ("navigate", "route", "directions"):
            if not origin or not destination:
                return "Sir, please specify both an origin and a destination for navigation."

            orig_results = search_locations(origin, limit=1)
            dest_results = search_locations(destination, limit=1)

            if not orig_results:
                return f"Sir, I could not identify the coordinates for origin location: {origin}."
            if not dest_results:
                return f"Sir, I could not identify the coordinates for destination location: {destination}."

            orig_pt = orig_results[0]
            dest_pt = dest_results[0]

            coords_list = [(orig_pt["longitude"], orig_pt["latitude"])]

            # Intermediate waypoints if provided
            for wp in waypoints:
                wp_res = search_locations(wp, limit=1)
                if wp_res:
                    coords_list.append((wp_res[0]["longitude"], wp_res[0]["latitude"]))

            coords_list.append((dest_pt["longitude"], dest_pt["latitude"]))

            # Asynchronous route calculation
            def _async_route_calc():
                route_res = calculate_route(coords_list, travel_mode=travel_mode)
                if route_res.get("success"):
                    dist_km = route_res.get("distance_km")
                    dur_text = route_res.get("duration_text")
                    turns = route_res.get("turn_by_turn", [])
                    manager.broadcast_command("show_route", route_res)
                    summary = (
                        f"Route: {orig_pt['name']} \u2192 {dest_pt['name']}\n"
                        f"Mode: {travel_mode.title()}\n"
                        f"Total Distance: {dist_km} km ({route_res.get('distance_miles')} mi)\n"
                        f"Estimated Travel Time: {dur_text}\n"
                        f"Guidance Steps: {len(turns)} turns\n"
                        f"Provider: {route_res.get('provider')}"
                    )
                    _log_and_hud(player, "ROUTE GUIDANCE", summary, f"Route calculated: {orig_pt['name']} to {dest_pt['name']}, {dist_km} km, {dur_text}.")

            threading.Thread(target=_async_route_calc, daemon=True).start()
            return f"Calculating the fastest {travel_mode} route from {orig_pt['name']} to {dest_pt['name']}, sir."

        # Action: Clear route
        if action in ("clear_route", "cancel_route"):
            manager.broadcast_command("clear_route")
            _log_and_hud(player, "ROUTE GUIDANCE", "Active route cleared.", "Route cleared.")
            return "Sir, I have cleared the active route from the map."

        # Action: Flight tracking
        if action in ("track_flight", "flights", "radar"):
            if callsign or icao24:
                manager.broadcast_command("track_aircraft", {"callsign": callsign, "icao24": icao24})
                target = callsign or icao24
                _log_and_hud(player, "FLIGHT RADAR", f"Tracking target: {target}\nProvider: OpenSky Network\nStatus: Scanning telemetry", f"Flight radar focused on {target}.")
                return f"Focusing flight radar on aircraft {target}, sir."
            else:
                manager.broadcast_command("enable_flight_radar", {"enabled": True})
                _log_and_hud(player, "FLIGHT RADAR", "Live OpenSky Network Radar: Enabled\nFiltering: Active Viewport", "Flight radar layer activated.")
                return "Live flight radar layer engaged, sir. Showing current aircraft positions."

        # Action: ISS tracking
        if action in ("track_iss", "iss", "space_station"):
            telemetry = get_iss_telemetry()
            manager.broadcast_command("track_iss", {"enabled": True, "follow": True})
            summary = (
                f"Position: {telemetry['latitude']}\u00b0, {telemetry['longitude']}\u00b0\n"
                f"Altitude: {telemetry['altitude_km']} km ({telemetry['altitude_miles']} mi)\n"
                f"Orbital Velocity: {telemetry['velocity_kmh']} km/h ({telemetry['velocity_mph']} mph)\n"
                f"Visibility: {telemetry['visibility'].title()}\n"
                f"Source: {telemetry['source']}"
            )
            _log_and_hud(player, "ISS TRACKER", summary, f"ISS tracking active at {telemetry['latitude']}, {telemetry['longitude']}.")
            return f"The International Space Station is currently at latitude {telemetry['latitude']}, longitude {telemetry['longitude']}, traveling at {telemetry['velocity_kmh']} kilometers per hour. Camera tracking enabled, sir."

        # Action: Search / Find location
        if action in ("search", "find", "locate") or loc:
            target_query = loc or f"{lat},{lon}"
            results = search_locations(target_query, limit=5)
            if not results:
                return f"Sir, I could not find any geographic matches for '{target_query}'."

            best = results[0]
            dms = decimal_to_dms(best["latitude"], best["longitude"])
            manager.broadcast_command("fly_to", {
                "latitude": best["latitude"],
                "longitude": best["longitude"],
                "zoom": parameters.get("zoom") or 12.0,
                "name": best["name"],
                "display_name": best.get("display_name"),
            })

            summary = (
                f"Location: {best['name']}\n"
                f"Full Name: {best.get('display_name')}\n"
                f"Coordinates: {best['latitude']}, {best['longitude']}\n"
                f"DMS: {dms['full_dms']}\n"
                f"Category: {best.get('category', 'Place')}\n"
                f"Source: {best.get('source', 'Geocoding')}"
            )
            _log_and_hud(player, "GEOGRAPHIC SEARCH", summary, f"Located {best['name']} at {dms['full_dms']}.")
            return f"Found {best['name']}, sir. Centering the map at {dms['full_dms']}."

        # Action: Discover destinations
        if action in ("discover_destinations", "destinations", "attractions"):
            cat = loc or "Historical places"
            dests = get_curated_destinations(cat)
            manager.broadcast_command("show_destinations", {"category": cat})
            count = len(dests)
            _log_and_hud(player, "DESTINATION DISCOVERY", f"Category: {cat}\nDestinations Found: {count}\nExplore curated sites on the map.", f"Surfaced {count} destinations in {cat}.")
            return f"Displaying {count} curated destinations for {cat} on the map, sir."

        # Action: Find nearby places (hotels, restaurants, hospitals, airports)
        if action in ("find_places", "nearby", "pois"):
            center_lat = float(lat) if lat is not None else 12.30814
            center_lon = float(lon) if lon is not None else 75.10632
            cat = loc or "attraction"
            places = search_places_nearby(cat, center_lat, center_lon, radius_km=25.0, limit=15)
            manager.broadcast_command("show_places", {"category": cat, "places": places})
            count = len(places)
            _log_and_hud(player, "NEARBY PLACES", f"Category: {cat}\nFound: {count} points of interest within 25 km", f"Found {count} {cat} locations nearby.")
            return f"Found {count} {cat} locations within 25 kilometers, displayed on the map, sir."

        # Default action: Open map explorer
        manager.broadcast_command("focus", {})
        _log_and_hud(player, "JARVIS MAP EXPLORER", f"Map Explorer Online\nLocal Server: http://127.0.0.1:{manager.port}\nStatus: Active in Main HUD", "Map Explorer application opened in main HUD.")
        return "Interactive Map Explorer is active in the main HUD display, sir. Ready for navigation, 3D globe visualization, or flight tracking."

    except Exception as exc:
        print(f"[MapExplorer Plugin Error] {exc}")
        return f"Sir, Map Explorer encountered an issue: {exc}"


def _log_and_hud(player: Any, title: str, text: str, log_msg: str) -> None:
    """Thread-safe logging and HUD display updates."""
    if not player:
        return
    try:
        player.write_log(f"JARVIS: {log_msg}")
    except Exception:
        pass
    try:
        player.show_content(title, text)
    except Exception:
        pass
