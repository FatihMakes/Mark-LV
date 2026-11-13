"""
Google Calendar API client wrapper for Calendar and Schedule Companion.
Retrieves agenda, detects scheduling conflicts, and creates confirmed calendar events.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Tuple

from plugins._google_auth import GoogleAuthManager, SCOPES_CALENDAR


class GoogleCalendarClient:
    """Safely executes calendar reads, agenda queries, and event creation."""

    @classmethod
    def get_service(cls, client_id: str, client_secret: str) -> Tuple[Optional[Any], str]:
        creds, msg = GoogleAuthManager.get_credentials("calendar", client_id, client_secret, SCOPES_CALENDAR)
        if not creds:
            return None, msg

        try:
            from googleapiclient.discovery import build
            service = build("calendar", "v3", credentials=creds, cache_discovery=False)
            return service, "Connected"
        except Exception as exc:
            return None, f"Failed to build Calendar service: {exc}"

    @classmethod
    def get_upcoming_events(cls, service: Any, max_results: int = 10, days_ahead: int = 7) -> List[Dict[str, Any]]:
        """Retrieve upcoming calendar events starting from now."""
        try:
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            time_max = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days_ahead)).isoformat()

            events_result = service.events().list(
                calendarId="primary",
                timeMin=now,
                timeMax=time_max,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime"
            ).execute()

            events = events_result.get("items", [])
            out = []
            for ev in events:
                start_raw = ev.get("start", {}).get("dateTime", ev.get("start", {}).get("date", ""))
                end_raw = ev.get("end", {}).get("dateTime", ev.get("end", {}).get("date", ""))
                out.append({
                    "id": ev.get("id"),
                    "summary": ev.get("summary", "(No Title)"),
                    "start": start_raw,
                    "end": end_raw,
                    "location": ev.get("location", ""),
                    "description": ev.get("description", "")
                })
            return out
        except Exception:
            return []

    @classmethod
    def create_event(
        cls,
        service: Any,
        summary: str,
        start_iso: str,
        end_iso: str,
        description: str = "",
        location: str = ""
    ) -> Tuple[bool, str]:
        """Create a new calendar event and return confirmed event link."""
        try:
            event_body = {
                "summary": summary,
                "location": location,
                "description": description,
                "start": {"dateTime": start_iso},
                "end": {"dateTime": end_iso},
            }
            created = service.events().insert(calendarId="primary", body=event_body).execute()
            html_link = created.get("htmlLink", "")
            return True, f"Event '{summary}' scheduled successfully (Link: {html_link})."
        except Exception as exc:
            return False, f"Failed to create event: {exc}"
