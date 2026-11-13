"""
Local loopback HTTP server and real-time event dispatcher for JARVIS Map Explorer.
Binds strictly to 127.0.0.1, enforces session token security, serves the web application,
and proxies geographic/telemetry API calls safely.
"""

from __future__ import annotations

import collections
import datetime
import http.server
import json
import mimetypes
import os
import pathlib
import queue
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from typing import Any, Dict, List, Optional, Tuple

from plugins._map_explorer_data import (
    decimal_to_dms,
    get_curated_destinations,
    get_earthquake_layer,
    get_weather_intelligence,
    parse_coordinates_input,
    reverse_geocode,
    search_locations,
    search_places_nearby,
)
from plugins._map_explorer_flights import get_aircraft_track, query_flights
from plugins._map_explorer_iss import (
    calculate_orbital_prediction,
    calculate_solar_terminator,
    get_iss_telemetry,
)
from plugins._map_explorer_routes import calculate_route

_SERVER_HOST = "127.0.0.1"
_PREFERRED_PORT = 5757
_WEB_DIR = pathlib.Path(__file__).parent / "map_explorer_web"


class MapExplorerServerManager:
    """Thread-safe singleton managing the local HTTP server instance."""

    _instance: Optional[MapExplorerServerManager] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.port: int = 0
        self.session_token: str = secrets.token_urlsafe(24)
        self.server: Optional[http.server.ThreadingHTTPServer] = None
        self.server_thread: Optional[threading.Thread] = None
        self.is_running: bool = False
        self.browser_opened: bool = False
        self.last_heartbeat: float = time.time()
        self.event_queues: List[queue.Queue] = []
        self.eq_lock = threading.Lock()
        self.recent_commands: collections.deque = collections.deque(maxlen=50)
        self.current_state: Dict[str, Any] = {
            "view": "2d",
            "layer": "streets",
            "center": [75.106, 12.308],  # default Kanhangad/Kasaragod region
            "zoom": 12.0,
            "pitch": 0.0,
            "bearing": 0.0,
            "active_route": None,
            "tracked_flight": None,
            "iss_following": False,
        }

    @classmethod
    def get_instance(cls) -> MapExplorerServerManager:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def start(self) -> str:
        """Start the server on 127.0.0.1 if not already running. Returns the access URL."""
        with self._lock:
            if self.is_running and self.server:
                return f"http://{_SERVER_HOST}:{self.port}/?token={self.session_token}"

            # Find available port
            port = _PREFERRED_PORT
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((_SERVER_HOST, port))
                sock.close()
            except OSError:
                # Preferred port is taken, bind to 0 to let OS assign free port
                sock.close()
                temp_s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                temp_s.bind((_SERVER_HOST, 0))
                port = temp_s.getsockname()[1]
                temp_s.close()

            self.port = port
            handler_factory = lambda *args, **kwargs: MapExplorerHTTPHandler(self, *args, **kwargs)

            try:
                self.server = http.server.ThreadingHTTPServer((_SERVER_HOST, port), handler_factory)
                self.server.daemon_threads = True
                self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="MapExplorerServer")
                self.server_thread.start()
                self.is_running = True
                print(f"[MapExplorer] Loopback server running at http://{_SERVER_HOST}:{self.port}")
            except Exception as exc:
                print(f"[MapExplorer] Failed to start server: {exc}")
                raise

            return f"http://{_SERVER_HOST}:{self.port}/?token={self.session_token}"

    def ensure_browser_open(self) -> str:
        """Ensure server is running. Map is displayed in the main HUD UI; external browser is suppressed."""
        return self.start()

    def broadcast_command(self, action: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Push a command to connected web browsers via SSE."""
        payload = {
            "type": "command",
            "action": action,
            "data": data or {},
            "timestamp": time.time(),
        }
        with self.eq_lock:
            self.recent_commands.append(payload)
            for q in list(self.event_queues):
                try:
                    q.put_nowait(payload)
                except Exception:
                    pass

    def register_client(self) -> queue.Queue:
        """Register a new SSE client queue."""
        q: queue.Queue = queue.Queue(maxsize=100)
        with self.eq_lock:
            self.event_queues.append(q)
            self.last_heartbeat = time.time()
        return q

    def unregister_client(self, q: queue.Queue) -> None:
        """Remove an SSE client queue."""
        with self.eq_lock:
            if q in self.event_queues:
                self.event_queues.remove(q)

    def stop(self) -> None:
        """Shutdown the local server."""
        with self._lock:
            if self.server:
                try:
                    self.server.shutdown()
                    self.server.server_close()
                except Exception:
                    pass
                self.server = None
                self.is_running = False
                self.browser_opened = False
                print("[MapExplorer] Local server stopped.")


class MapExplorerHTTPHandler(http.server.BaseHTTPRequestHandler):
    """Secure request handler strictly serving local web assets and API endpoints."""

    def __init__(self, manager: MapExplorerServerManager, *args, **kwargs):
        self.manager = manager
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress routine HTTP log spam in the terminal
        pass

    def _validate_token(self, query_params: Dict[str, List[str]]) -> bool:
        """Verify request authenticity via token header, query param, or loopback origin."""
        req_token = self.headers.get("X-Session-Token")
        if req_token == self.manager.session_token:
            return True
        param_tokens = query_params.get("token", [])
        if param_tokens and param_tokens[0] == self.manager.session_token:
            return True
        # For browser asset sub-resources, check referrer containing valid token
        referer = self.headers.get("Referer", "")
        if f"token={self.manager.session_token}" in referer:
            return True
        return False

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # Server-Sent Events endpoint
        if path == "/api/events":
            if not self._validate_token(query):
                self._send_json({"error": "Unauthorized session token"}, status=401)
                return
            self._handle_sse()
            return

        # Poll endpoint (fallback for environments where SSE might block)
        if path == "/api/poll":
            if not self._validate_token(query):
                self._send_json({"error": "Unauthorized session token"}, status=401)
                return
            recent = list(self.manager.recent_commands)
            self._send_json({"commands": recent, "state": self.manager.current_state})
            return

        # API routing
        if path.startswith("/api/"):
            if not self._validate_token(query):
                self._send_json({"error": "Unauthorized session token"}, status=401)
                return
            self._dispatch_api_get(path, query)
            return

        # Static web assets
        self._serve_static_file(path, query)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if not self._validate_token(query):
            self._send_json({"error": "Unauthorized session token"}, status=401)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_length > 0:
            try:
                raw_data = self.rfile.read(content_length).decode("utf-8")
                body = json.loads(raw_data)
            except Exception:
                body = {}

        if path == "/api/command":
            action = body.get("action", "")
            data = body.get("data", {})
            self.manager.broadcast_command(action, data)
            self._send_json({"success": True, "action": action})
            return

        if path == "/api/state":
            with self.manager._lock:
                for k, v in body.items():
                    if k in self.manager.current_state:
                        self.manager.current_state[k] = v
            self._send_json({"success": True, "state": self.manager.current_state})
            return

        if path == "/api/routes":
            coords_raw = body.get("coordinates", [])
            travel_mode = body.get("travel_mode", "driving")
            provider = body.get("provider", "osrm")
            api_key = body.get("api_key", "")
            custom_url = body.get("custom_url", "")

            coords: List[Tuple[float, float]] = []
            for pt in coords_raw:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    coords.append((float(pt[0]), float(pt[1])))

            result = calculate_route(coords, travel_mode, provider, api_key, custom_url)
            self._send_json(result)
            return

        if path == "/api/session/close":
            self.manager.broadcast_command("session_closed", {})
            self._send_json({"success": True, "message": "Map session flagged closed."})
            return

        self._send_json({"error": f"Endpoint not found: {path}"}, status=404)

    def _dispatch_api_get(self, path: str, query: Dict[str, List[str]]) -> None:
        """Route GET API queries to specialized intelligence modules."""
        if path == "/api/status":
            self._send_json({
                "status": "online",
                "port": self.manager.port,
                "uptime": time.time() - self.manager.last_heartbeat,
                "version": "MARK-LVII-1.0",
                "features": {
                    "2d_map": True,
                    "3d_globe": True,
                    "iss_tracking": True,
                    "flight_tracking": True,
                    "routing": True,
                    "places": True,
                    "earthquakes": True,
                    "weather": True,
                },
            })
            return

        if path == "/api/state":
            self._send_json(self.manager.current_state)
            return

        if path == "/api/geocode":
            q = query.get("q", [""])[0]
            limit = int(query.get("limit", ["10"])[0])
            results = search_locations(q, limit=limit)
            self._send_json({"query": q, "results": results})
            return

        if path == "/api/reverse":
            lat = float(query.get("lat", ["0.0"])[0])
            lon = float(query.get("lon", ["0.0"])[0])
            res = reverse_geocode(lat, lon)
            dms = decimal_to_dms(lat, lon)
            res["dms"] = dms
            self._send_json(res)
            return

        if path == "/api/places":
            cat = query.get("category", [""])[0]
            lat = float(query.get("lat", ["0.0"])[0])
            lon = float(query.get("lon", ["0.0"])[0])
            radius = float(query.get("radius", ["25.0"])[0])
            limit = int(query.get("limit", ["20"])[0])
            places = search_places_nearby(cat, lat, lon, radius, limit)
            self._send_json({"category": cat, "center": [lon, lat], "places": places})
            return

        if path == "/api/destinations":
            cat = query.get("category", [""])[0]
            destinations = get_curated_destinations(cat)
            self._send_json({"destinations": destinations})
            return

        if path == "/api/flights":
            lamin = float(query.get("lamin", [None])[0]) if "lamin" in query else None
            lomin = float(query.get("lomin", [None])[0]) if "lomin" in query else None
            lamax = float(query.get("lamax", [None])[0]) if "lamax" in query else None
            lomax = float(query.get("lomax", [None])[0]) if "lomax" in query else None
            callsign = query.get("callsign", [None])[0]
            icao24 = query.get("icao24", [None])[0]
            limit = int(query.get("limit", ["250"])[0])

            # Get OpenSky credentials from memory/config_manager if stored
            from memory.config_manager import get_plugin_setting
            client_id = get_plugin_setting("map_explorer", "opensky_client_id", "") or ""
            client_secret = get_plugin_setting("map_explorer", "opensky_client_secret", "") or ""

            res = query_flights(
                lamin=lamin,
                lomin=lomin,
                lamax=lamax,
                lomax=lomax,
                callsign=callsign,
                icao24=icao24,
                client_id=client_id,
                client_secret=client_secret,
                limit=limit,
            )
            self._send_json(res)
            return

        if path == "/api/flights/track":
            icao24 = query.get("icao24", [""])[0]
            from memory.config_manager import get_plugin_setting
            client_id = get_plugin_setting("map_explorer", "opensky_client_id", "") or ""
            client_secret = get_plugin_setting("map_explorer", "opensky_client_secret", "") or ""
            track = get_aircraft_track(icao24, client_id, client_secret)
            self._send_json(track)
            return

        if path == "/api/iss":
            telemetry = get_iss_telemetry()
            self._send_json(telemetry)
            return

        if path == "/api/iss/orbit":
            lat = float(query.get("lat", ["0.0"])[0])
            lon = float(query.get("lon", ["0.0"])[0])
            mins = int(query.get("minutes", ["95"])[0])
            prediction = calculate_orbital_prediction(lat, lon, minutes_ahead=mins)
            self._send_json(prediction)
            return

        if path == "/api/layers/earthquakes":
            feed = query.get("feed", ["2.5_day"])[0]
            quakes = get_earthquake_layer(feed)
            self._send_json(quakes)
            return

        if path == "/api/layers/terminator":
            epoch = float(query.get("time", [time.time()])[0])
            term = calculate_solar_terminator(epoch)
            self._send_json(term)
            return

        if path == "/api/layers/weather":
            lat = float(query.get("lat", ["0.0"])[0])
            lon = float(query.get("lon", ["0.0"])[0])
            weather = get_weather_intelligence(lat, lon)
            self._send_json(weather)
            return

        if path == "/api/settings":
            from memory.config_manager import get_plugin_config
            cfg = get_plugin_config("map_explorer")
            # Sanitize password fields for client display
            safe_cfg = dict(cfg)
            for k in ("map_api_key", "routing_api_key", "places_api_key", "opensky_client_secret"):
                if safe_cfg.get(k):
                    safe_cfg[k] = "••••••••"
            self._send_json(safe_cfg)
            return

        self._send_json({"error": f"Unknown API path: {path}"}, status=404)

    def _serve_static_file(self, path: str, query: Dict[str, List[str]]) -> None:
        """Serve HTML, CSS, JavaScript and vendor resources from map_explorer_web."""
        clean_path = path.strip("/")
        if not clean_path or clean_path == "index.html":
            # For root index.html, check token
            if not self._validate_token(query):
                self._send_response_simple(401, "text/plain", b"Unauthorized: Missing or invalid session token.")
                return
            target_file = _WEB_DIR / "index.html"
        else:
            # Resolve relative to _WEB_DIR and avoid directory traversal
            try:
                target_file = (_WEB_DIR / clean_path).resolve()
                if not str(target_file).startswith(str(_WEB_DIR.resolve())):
                    self._send_response_simple(403, "text/plain", b"Forbidden path")
                    return
            except Exception:
                self._send_response_simple(404, "text/plain", b"Not Found")
                return

        if not target_file.is_file():
            self._send_response_simple(404, "text/plain", f"File not found: {clean_path}".encode("utf-8"))
            return

        mime_type, _ = mimetypes.guess_type(str(target_file))
        if not mime_type:
            if target_file.suffix == ".js":
                mime_type = "application/javascript"
            elif target_file.suffix == ".css":
                mime_type = "text/css"
            elif target_file.suffix == ".json":
                mime_type = "application/json"
            else:
                mime_type = "application/octet-stream"

        try:
            with open(target_file, "rb") as f:
                content = f.read()

            self.send_response(200)
            self.send_header("Content-Type", f"{mime_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache, must-revalidate")
            self.send_header("Access-Control-Allow-Origin", f"http://{_SERVER_HOST}:{self.manager.port}")
            self.end_headers()
            self.wfile.write(content)
        except Exception as exc:
            print(f"[MapExplorer HTTP] Static file delivery error: {exc}")

    def _handle_sse(self) -> None:
        """Stream real-time server events to the browser client."""
        client_q = self.manager.register_client()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", f"http://{_SERVER_HOST}:{self.manager.port}")
        self.end_headers()

        # Send initial connected message
        init_msg = json.dumps({"type": "connected", "token": self.manager.session_token, "state": self.manager.current_state})
        self.wfile.write(f"data: {init_msg}\n\n".encode("utf-8"))
        self.wfile.flush()

        try:
            while self.manager.is_running:
                try:
                    payload = client_q.get(timeout=15.0)
                    data_str = json.dumps(payload)
                    self.wfile.write(f"data: {data_str}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # Keep-alive heartbeat comment
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.manager.unregister_client(client_q)

    def _send_json(self, data: Any, status: int = 200) -> None:
        content = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Access-Control-Allow-Origin", f"http://{_SERVER_HOST}:{self.manager.port}")
        self.end_headers()
        self.wfile.write(content)

    def _send_response_simple(self, code: int, mime: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
