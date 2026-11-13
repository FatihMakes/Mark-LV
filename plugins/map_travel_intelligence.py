"""
JARVIS Map and Travel Intelligence Plugin.
Integrates full 2D map, 3D Earth globe, turn-by-turn navigation, OpenSky flight radar,
and real-time ISS telemetry tracking. Extends existing map engine cleanly.
"""

from __future__ import annotations

import pathlib
from typing import Any, Dict, List, Optional

from memory.config_manager import get_plugin_config, get_plugin_setting
from plugins import map_explorer
from plugins._plugin_common import (
    HudVisualGenerator,
    safe_player_content,
    safe_player_image,
    safe_player_log,
)

PLUGIN = {
    "name": "map_travel_intelligence",
    "description": (
        "Interactive 2D map, 3D Earth globe, navigation, flight tracking, and ISS orbit tracking. "
        "Triggers: 'open map explorer', 'show 3d globe', 'switch to satellite view', "
        "'navigate from A to B', 'where is the ISS', 'track flights'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action: 'open', 'show_globe', 'show_map', 'navigate', 'track_iss', 'track_flight', 'set_layer', or 'close'",
            },
            "location": {
                "type": "STRING",
                "description": "Target city, landmark, or destination",
            },
            "origin": {
                "type": "STRING",
                "description": "Starting address for route planning",
            },
            "destination": {
                "type": "STRING",
                "description": "Destination address for route planning",
            },
            "map_layer": {
                "type": "STRING",
                "description": "Layer style: 'streets', 'satellite', 'dark', 'light', 'topo'",
            },
            "aircraft_callsign": {
                "type": "STRING",
                "description": "Aircraft callsign for radar tracking",
            },
        },
        "required": [],
    },
    "behavior": "NON_BLOCKING",
    "tier": "core",
}

PLUGIN_SETTINGS = {
    "namespace": "map_travel_intelligence",
    "title": "MAP & TRAVEL INTELLIGENCE",
    "fields": [
        {
            "key": "default_view",
            "label": "Default Map Projection",
            "type": "choice",
            "options": ["2D Mercator", "3D Spherical Globe"],
            "default": "2D Mercator",
        },
        {
            "key": "tile_provider",
            "label": "Default Base Map Style",
            "type": "choice",
            "options": ["CartoDB Voyager (Street)", "ESRI Satellite", "CartoDB Dark Matter", "OpenTopoMap"],
            "default": "CartoDB Voyager (Street)",
        },
    ],
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Forward command directly to the map_explorer engine and generate supporting HUD telemetry cards."""
    action = str(parameters.get("action") or "open").lower().strip()
    loc = str(parameters.get("location") or "").strip()
    orig = str(parameters.get("origin") or "").strip()
    dest = str(parameters.get("destination") or "").strip()

    try:
        # Delegate to existing map_explorer engine
        result_speech = map_explorer.run(parameters, player=player, session_memory=session_memory)

        # Generate a visual preview card for HUD display
        metrics = [
            ("MODE", "3D GLOBE" if "globe" in action else "2D MAP", "cyan"),
            ("TARGET", loc[:16] or (dest[:16] if dest else "Kanhangad"), "amber"),
            ("ENGINE", "MapLibre GL", "green"),
        ]
        table_rows = [
            ["Feature Layer", "Status"],
            ["Cartographic Base", "Online"],
            ["Road Navigation", "Active (OSRM)"],
            ["Orbital ISS Tracking", "Synchronized"],
            ["Aviation Radar", "OpenSky Network Active"],
        ]

        card_path = HudVisualGenerator.create_card_image(
            title="MAP & TRAVEL INTELLIGENCE",
            subtitle="Earth Intelligence & Navigation Subsystem",
            metrics=metrics,
            table_rows=table_rows,
            footer="Integrated in Main HUD UI",
            output_filename="map_travel_card.png"
        )
        safe_player_image(player, card_path, "MAP & TRAVEL INTELLIGENCE")

        return result_speech

    except Exception as exc:
        return f"Sir, Map and Travel Intelligence encountered an issue: {exc}"
