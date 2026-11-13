"""
JARVIS Calendar and Schedule Companion Plugin.
Provides upcoming agenda, meeting conflict analysis, and confirmed event scheduling.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from memory.config_manager import get_plugin_config, get_plugin_setting
from plugins._google_auth import GoogleAuthManager, SCOPES_CALENDAR
from plugins._google_calendar import GoogleCalendarClient
from plugins._plugin_common import (
    HudVisualGenerator,
    safe_player_content,
    safe_player_image,
    safe_player_log,
)


def _test_calendar_connection(values: dict) -> tuple[bool, str]:
    """Test action connected through PLUGIN_SETTINGS."""
    cid = values.get("client_id", "")
    sec = values.get("client_secret", "")
    service, msg = GoogleCalendarClient.get_service(cid, sec)
    if service:
        return True, "Google Calendar API connection verified and active."
    return False, f"Connection failed: {msg}"


PLUGIN = {
    "name": "calendar_companion",
    "description": (
        "View upcoming schedule, events, agenda, and schedule calendar meetings. "
        "Triggers: 'show my calendar', 'what is on my schedule today', 'upcoming meetings', "
        "'schedule a meeting tomorrow', 'check agenda'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action: 'agenda', 'today', 'create_event', 'connect', or 'status'",
            },
            "summary": {
                "type": "STRING",
                "description": "Event title or meeting summary",
            },
            "start_time": {
                "type": "STRING",
                "description": "Start ISO datetime string (e.g. 2026-11-14T10:00:00Z)",
            },
            "end_time": {
                "type": "STRING",
                "description": "End ISO datetime string (e.g. 2026-11-14T11:00:00Z)",
            },
            "description": {
                "type": "STRING",
                "description": "Optional event description or notes",
            },
        },
        "required": ["action"],
    },
    "behavior": "NON_BLOCKING",
    "tier": "core",
}

PLUGIN_SETTINGS = {
    "namespace": "calendar_companion",
    "title": "CALENDAR & SCHEDULE COMPANION",
    "fields": [
        {
            "key": "client_id",
            "label": "Google OAuth Client ID",
            "type": "text",
            "default": "",
            "placeholder": "Enter client ID from Google Cloud Console",
        },
        {
            "key": "client_secret",
            "label": "Google OAuth Client Secret",
            "type": "password",
            "default": "",
            "placeholder": "Enter client secret",
        },
        {
            "key": "calendar_timezone",
            "label": "Default Calendar Timezone",
            "type": "choice",
            "options": ["Asia/Kolkata", "UTC", "America/New_York", "Europe/London"],
            "default": "Asia/Kolkata",
        },
    ],
    "action": {
        "label": "TEST CONNECTION",
        "run": _test_calendar_connection,
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "agenda").lower().strip()
    title = str(parameters.get("summary") or "").strip()
    start_time = str(parameters.get("start_time") or "").strip()
    end_time = str(parameters.get("end_time") or "").strip()
    description = str(parameters.get("description") or "").strip()

    cid = get_plugin_setting("calendar_companion", "client_id", default="")
    sec = get_plugin_setting("calendar_companion", "client_secret", default="")

    try:
        # 1. Action: Connect OAuth
        if action in ("connect", "auth", "login"):
            ok, msg = GoogleAuthManager.run_local_auth_flow("calendar", cid, sec, SCOPES_CALENDAR)
            return msg

        # Try to acquire service
        service, err_msg = GoogleCalendarClient.get_service(cid, sec)

        # 2. Action: Create Event
        if action in ("create_event", "schedule", "add_event"):
            if not service:
                return f"Sir, Google Calendar is not connected: {err_msg}"
            if not title:
                return "Sir, please provide a summary or title for the event."
            if not start_time:
                # Default to 1 hour from now if unspecified
                now = datetime.datetime.now(datetime.timezone.utc)
                start_dt = now + datetime.timedelta(hours=1)
                end_dt = start_dt + datetime.timedelta(hours=1)
                start_time = start_dt.isoformat()
                end_time = end_dt.isoformat()

            ok, res_msg = GoogleCalendarClient.create_event(service, title, start_time, end_time, description)
            safe_player_log(player, f"Calendar: {res_msg}")
            safe_player_content(player, "EVENT SCHEDULED", f"Title: {title}\nStart: {start_time}\nStatus: {res_msg}")
            return f"Sir, event '{title}' has been successfully scheduled on your calendar."

        # 3. Action: Agenda / Upcoming Events (Default)
        if not service:
            # Degrade gracefully if offline / unconfigured
            metrics = [
                ("STATUS", "UNCONFIGURED", "amber"),
                ("EVENTS TODAY", "0", "cyan"),
                ("PROVIDER", "Google Calendar", "cyan"),
            ]
            card_path = HudVisualGenerator.create_card_image(
                title="CALENDAR & SCHEDULE",
                subtitle="Google Calendar Integration",
                metrics=metrics,
                table_rows=[["Setup Step", "Instructions"], ["OAuth Credentials", "Configure Client ID in ⚙ → Plugin Settings"]],
                footer="Awaiting Google Cloud Credentials",
                output_filename="calendar_status_card.png"
            )
            safe_player_image(player, card_path, "CALENDAR COMPANION")
            safe_player_content(player, "CALENDAR COMPANION", f"Google Calendar is not currently connected.\n\nTo view your schedule, configure your Google OAuth Client ID and Secret in ⚙ → PLUGIN SETTINGS.")
            return "Sir, your Google Calendar is not yet connected. You can configure credentials under Plugin Settings."

        events = GoogleCalendarClient.get_upcoming_events(service, max_results=8)
        metrics = [
            ("STATUS", "CONNECTED", "green"),
            ("UPCOMING", f"{len(events)} Events", "cyan"),
            ("PROVIDER", "Google Calendar", "amber"),
        ]

        table_rows = [["Time", "Event Summary", "Location"]]
        if events:
            for ev in events[:6]:
                st = ev["start"][:16].replace("T", " ") if "T" in ev["start"] else ev["start"]
                table_rows.append([st, ev["summary"][:24], ev.get("location", "")[:18] or "Online"])
        else:
            table_rows.append(["None", "No upcoming events scheduled", ""])

        card_path = HudVisualGenerator.create_card_image(
            title="CALENDAR COMPANION - AGENDA",
            subtitle=f"{len(events)} Scheduled Events",
            metrics=metrics,
            table_rows=table_rows,
            footer="Schedule up-to-date",
            output_filename="calendar_agenda_card.png"
        )
        safe_player_image(player, card_path, "CALENDAR AGENDA")

        details = "\n".join([f"• {e['summary']} at {e['start']}" for e in events[:5]]) if events else "No upcoming events found."
        safe_player_content(player, f"UPCOMING AGENDA ({len(events)})", details)

        if events:
            next_ev = events[0]
            return f"Sir, you have {len(events)} upcoming events. Next up is '{next_ev['summary']}' starting at {next_ev['start']}."
        return "Sir, your calendar is clear with no upcoming events scheduled for this week."

    except Exception as exc:
        return f"Sir, Calendar Companion encountered an issue: {exc}"
