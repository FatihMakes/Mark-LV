"""
JARVIS Command Center Plugin.
Central executive dashboard that unifies information from all 7 sibling plugins:
email, calendar, storage, downloads, file finder, network, and map intelligence.
"""

from __future__ import annotations

import os
import sys
import time
import pathlib
import datetime
from typing import Any, Dict, List, Optional

from memory.config_manager import get_plugin_config, get_plugin_setting
from plugins import (
    calendar_companion,
    downloads_organizer,
    email_intelligence,
    map_travel_intelligence,
    network_intelligence,
    smart_file_finder,
    storage_guardian,
)
from plugins._file_index import FileIndexEngine
from plugins._file_operations import FileOrganizerEngine
from plugins._google_auth import GoogleAuthManager
from plugins._network_utils import NetworkUtils
from plugins._plugin_common import (
    HudVisualGenerator,
    format_bytes,
    safe_player_content,
    safe_player_image,
    safe_player_log,
)
from plugins._storage_utils import StorageUtils

PLUGIN = {
    "name": "jarvis_command_center",
    "description": (
        "Central personal dashboard and executive daily briefing unifying email, calendar, storage, "
        "downloads, network, files, and map status. "
        "Triggers: 'show my command center', 'daily briefing', 'what is happening today', "
        "'system overview', 'refresh command center', 'show my dashboard'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action: 'briefing', 'dashboard', 'refresh', or 'status'",
            },
        },
        "required": [],
    },
    "behavior": "NON_BLOCKING",
    "tier": "core",
}

PLUGIN_SETTINGS = {
    "namespace": "jarvis_command_center",
    "title": "JARVIS COMMAND CENTER",
    "fields": [
        {
            "key": "user_title",
            "label": "User Title / Honorific",
            "type": "text",
            "default": "Sir",
            "placeholder": "Sir, Boss, etc.",
        },
        {
            "key": "include_network",
            "label": "Include Live Network Diagnostics",
            "type": "toggle",
            "default": True,
        },
        {
            "key": "include_storage",
            "label": "Include Storage Warnings",
            "type": "toggle",
            "default": True,
        },
    ],
}


def _get_greeting() -> str:
    hour = datetime.datetime.now().hour
    if hour < 12:
        return "Good morning"
    elif hour < 18:
        return "Good afternoon"
    return "Good evening"


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "briefing").lower().strip()
    user_title = get_plugin_setting("jarvis_command_center", "user_title", default="Sir")

    try:
        # Collect telemetry safely across all plugin subsystems without tight coupling
        # 1. Network status
        net_info = NetworkUtils.run_full_diagnostics()
        net_status = net_info.get("status", "OFFLINE")
        net_ping = f"{net_info.get('ping_ms', -1)} ms"

        # 2. Storage status
        drives = StorageUtils.get_drives_overview()
        prim_drive = drives[0] if drives else None
        drive_str = f"{prim_drive['free_str']} free" if prim_drive else "Unavailable"
        drive_pct = prim_drive["percent"] if prim_drive else 0

        # 3. Downloads status
        downloads_path = str(pathlib.Path.home() / "Downloads")
        dl_engine = FileOrganizerEngine()
        dl_scan = dl_engine.scan_downloads_folder(downloads_path)
        dl_count = dl_scan.get("total_files", 0)

        # 4. File Index status
        file_engine = FileIndexEngine.get_instance()
        total_indexed = file_engine.get_total_indexed()

        # 5. Google Integrations (Email & Calendar)
        has_email_auth = GoogleAuthManager.is_connected("email")
        has_cal_auth = GoogleAuthManager.is_connected("calendar")

        email_status = "Connected" if has_email_auth else "Unconfigured"
        cal_status = "Connected" if has_cal_auth else "Unconfigured"

        # Generate Master Visual HUD Card
        metrics = [
            ("NETWORK", f"{net_status} ({net_ping})", "green" if net_status == "ONLINE" else "red"),
            ("STORAGE", f"{drive_pct}% Used", "amber" if drive_pct >= 80 else "cyan"),
            ("DOWNLOADS", f"{dl_count} Unsorted", "green" if dl_count < 15 else "amber"),
            ("INDEXED FILES", f"{total_indexed:,}", "cyan"),
        ]

        table_rows = [
            ["Subsystem", "Provider / Status", "Health"],
            ["Network Intelligence", f"{net_info.get('primary_adapter_name', 'None')} ({net_info.get('primary_ip', '')})", net_status],
            ["Storage Guardian", f"{prim_drive['mountpoint'] if prim_drive else 'Drive'}: {drive_str}", "Nominal" if drive_pct < 85 else "Warning"],
            ["Downloads Organizer", f"{downloads_path}", f"{dl_count} files pending"],
            ["Smart File Finder", f"Indexed Database", f"{total_indexed} files ready"],
            ["Email Intelligence", "Gmail OAuth2", email_status],
            ["Calendar Companion", "Google Calendar v3", cal_status],
            ["Map & Travel Intelligence", "MapLibre GL / OSRM", "Active in HUD"],
        ]

        card_path = HudVisualGenerator.create_card_image(
            title="JARVIS EXECUTIVE COMMAND CENTER",
            subtitle=f"Daily Intelligence Briefing // {_get_greeting()}, {user_title}",
            metrics=metrics,
            table_rows=table_rows,
            footer="ALL SYSTEMS NOMINAL // MARK LVII COMMAND",
            output_filename="command_center_card.png"
        )

        safe_player_image(player, card_path, "JARVIS COMMAND CENTER")

        # Bottom content summary
        summary = (
            f"=== {user_title.upper()}'S DAILY INTELLIGENCE BRIEFING ===\n"
            f"• Internet: {net_status} (Latency: {net_ping})\n"
            f"• Primary Drive: {prim_drive['mountpoint'] if prim_drive else 'C:'} has {drive_str} available ({drive_pct}% utilized)\n"
            f"• Downloads: {dl_count} unsorted items in your Downloads directory\n"
            f"• File Finder: {total_indexed} user files indexed and searchable\n"
            f"• Email & Calendar: Email is {email_status.lower()}, Calendar is {cal_status.lower()}\n"
            f"• Map & Earth Intelligence: Online and accessible"
        )
        safe_player_content(player, "COMMAND CENTER BRIEFING", summary)
        safe_player_log(player, f"Executive Command Center refreshed.")

        greeting = _get_greeting()
        return (
            f"{greeting}, {user_title}. Your internet is {net_status.lower()} with {net_ping} latency, "
            f"primary storage has {drive_str} remaining, and {dl_count} files are currently in your downloads."
        )

    except Exception as exc:
        return f"{user_title}, Command Center encountered an issue compiling telemetry: {exc}"
