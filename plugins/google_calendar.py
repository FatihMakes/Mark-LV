"""Create, read, search, update, and delete Google Calendar events."""

import json
from datetime import datetime, timezone
from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

_CONFIG = Path(__file__).resolve().parent.parent / "config"
_CLIENT = _CONFIG / "client_secret.json"
_TOKEN = _CONFIG / "token_calendar.json"

_SCOPES = ["https://www.googleapis.com/auth/calendar"] 

PLUGIN = {
    "name": "google_calendar",
    "description": (
        "Manage Google Calendar events and reminders: create new appointments or timed reminders "
        "(with push notifications), read upcoming events, search by keyword, update, and delete events."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {
                "type": "STRING", 
                "enum": ["create", "read", "search", "update", "delete"]
            },
            "summary": {"type": "STRING", "description": "Title or summary of the event (for create or update)."},
            "start_time": {"type": "STRING", "description": "Start time in ISO format (e.g., 2026-10-01T14:00:00)."},
            "end_time": {"type": "STRING", "description": "End time in ISO format (e.g., 2026-10-01T15:00:00)."},
            "event_id": {"type": "STRING", "description": "Specific event identifier (required for update or delete)."},
            "query": {"type": "STRING", "description": "Search keyword for finding specific events (for search)."},
        },
        "required": ["operation"],
    },
}

PLUGIN_SETTINGS = {
    "title": "GOOGLE CALENDAR",
    "fields": [],
    "action": {"label": "CONNECT CALENDAR", "run": lambda _values: connect()},
}

def _save_token(creds: Credentials) -> None:
    _CONFIG.mkdir(parents=True, exist_ok=True)
    temporary = _TOKEN.with_name("token_calendar.tmp.json")
    temporary.write_text(creds.to_json(), encoding="utf-8")
    temporary.replace(_TOKEN)

def connect() -> tuple[bool, str]:
    if not _CLIENT.is_file():
        return False, "Put your Desktop OAuth JSON at config/client_secret.json."
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(_CLIENT), _SCOPES)
        creds = flow.run_local_server(port=0)
        _save_token(creds)
    except Exception as e:
        return False, f"Google Calendar connection failed: {e}"
    return True, "Google Calendar connected successfully."

def _credentials() -> Credentials | None:
    if not _TOKEN.is_file():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(_TOKEN), _SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(creds)
        return creds if creds.valid else None
    except Exception:
        return None

def run(parameters: dict) -> str:
    creds = _credentials()
    if creds is None:
        return "Reconnect Google Calendar in Plugin Settings to grant access."
    
    operation = parameters.get("operation", "")
    summary = str(parameters.get("summary", "")).strip()
    start_time = str(parameters.get("start_time", "")).strip()
    end_time = str(parameters.get("end_time", "")).strip()
    event_id = str(parameters.get("event_id", "")).strip()
    query = str(parameters.get("query", "")).strip()
    
    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        
        calendar_info = service.calendars().get(calendarId="primary").execute()
        user_timezone = calendar_info.get("timeZone", "UTC")
        
        if operation == "create":
            if not summary or not start_time or not end_time:
                return "Sir, please provide a summary, start_time, and end_time to create an event."
            
            clean_start = start_time.replace("Z", "").split("+")[0]
            clean_end = end_time.replace("Z", "").split("+")[0]
            
            event_body = {
                "summary": summary,
                "start": {"dateTime": clean_start, "timeZone": user_timezone},
                "end": {"dateTime": clean_end, "timeZone": user_timezone},
                "reminders": {
                    "useDefault": False,
                    "overrides": [
                        {"method": "popup", "minutes": 0},
                    ],
                },
            }
            
            result = service.events().insert(calendarId="primary", body=event_body).execute()
            return f"Sir, I scheduled '{summary}' for you (ID: {result.get('id')})."
            
        elif operation == "read":
            time_min = start_time if start_time else datetime.now(timezone.utc).isoformat()
            
            events_result = service.events().list(
                calendarId="primary", timeMin=time_min, maxResults=10, singleEvents=True,
                orderBy="startTime"
            ).execute()
            events = events_result.get("items", [])
            
            if not events:
                return "Sir, no upcoming events found in your calendar."
            
            formatted = []
            for event in events:
                raw_start = event["start"].get("dateTime", event["start"].get("date"))
                try:
                    dt = datetime.fromisoformat(raw_start)
                    start_formatted = dt.strftime("%d.%m.%Y um %H:%M Uhr")
                except ValueError:
                    start_formatted = raw_start
                
                event_summary = event.get('summary', 'Untitled')
                event_id_val = event.get('id', '')
                formatted.append(f"- {event_summary} am {start_formatted} (ID: {event_id_val})")
                
            return "Sir, here are your next upcoming events:\n" + "\n".join(formatted)

        elif operation == "search":
            if not query:
                return "Sir, please provide a search query to look up specific events."
            
            events_result = service.events().list(
                calendarId="primary", q=query, singleEvents=True, orderBy="startTime"
            ).execute()
            events = events_result.get("items", [])
            
            if not events:
                return f"Sir, I couldn't find any events matching '{query}'."
            
            formatted = []
            for event in events:
                raw_start = event["start"].get("dateTime", event["start"].get("date"))
                try:
                    dt = datetime.fromisoformat(raw_start)
                    start_formatted = dt.strftime("%d.%m.%Y um %H:%M Uhr")
                except ValueError:
                    start_formatted = raw_start
                
                event_summary = event.get('summary', 'Untitled')
                event_id_val = event.get('id', '')
                formatted.append(f"- {event_summary} am {start_formatted} (ID: {event_id_val})")
                
            return f"Sir, here are the events matching '{query}':\n" + "\n".join(formatted)
            
        elif operation == "update":
            if not event_id:
                return "Sir, please provide an event_id to update."
            
            event = service.events().get(calendarId="primary", eventId=event_id).execute()
            
            if summary:
                event["summary"] = summary
            if start_time:
                clean_start = start_time.replace("Z", "").split("+")[0]
                event["start"] = {"dateTime": clean_start, "timeZone": user_timezone}
            if end_time:
                clean_end = end_time.replace("Z", "").split("+")[0]
                event["end"] = {"dateTime": clean_end, "timeZone": user_timezone}
            
            event["reminders"] = {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 0},
                ],
            }
            
            updated_event = service.events().update(
                calendarId="primary", eventId=event_id, body=event
            ).execute()
            
            return f"Sir, I successfully updated the event '{updated_event.get('summary')}' (ID: {event_id})."
            
        elif operation == "delete":
            if not event_id:
                return "Sir, please provide an event_id to delete."
            service.events().delete(calendarId="primary", eventId=event_id).execute()
            return f"Sir, I deleted the calendar event with ID {event_id}."
            
        return "Unsupported Calendar operation."
    except Exception as e:
        return f"Google Calendar operation failed: {e}"