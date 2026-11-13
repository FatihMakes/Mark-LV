/**
 * Master Application Coordinator for JARVIS Map Explorer & 3D Earth Intelligence.
 * Wires modular sub-systems, real-time SSE stream, voice dispatching, and HUD events.
 */

import { MapEngine } from './modules/map.js';
import { GlobeManager } from './modules/globe.js';
import { RoutesManager } from './modules/routes.js';
import { PlacesManager } from './modules/places.js';
import { FlightsManager } from './modules/flights.js';
import { ISSManager } from './modules/iss.js';
import { LayersManager } from './modules/layers.js';
import { ControlsManager } from './modules/controls.js';
import { SearchManager } from './modules/search.js';
import { SettingsManager } from './modules/settings.js';

class MapExplorerApp {
    constructor() {
        // Extract session token from URL or session storage
        const params = new URLSearchParams(window.location.search);
        this.token = params.get('token') || sessionStorage.getItem('jarvis_map_token') || '';
        if (this.token) {
            sessionStorage.setItem('jarvis_map_token', this.token);
        }

        this.mapEngine = null;
        this.globeManager = null;
        this.routesManager = null;
        this.placesManager = null;
        this.flightsManager = null;
        this.issManager = null;
        this.layersManager = null;
        this.controlsManager = null;
        this.searchManager = null;
        this.settingsManager = null;

        this.eventSource = null;
    }

    async init() {
        console.log('[JARVIS Map Explorer] Initializing Earth Intelligence System...');

        // 1. Initialize core MapLibre engine (Default center: Kanhangad/Kasaragod region)
        this.mapEngine = new MapEngine('map', {
            projection: 'mercator',
            styleKey: 'dark'
        });
        const map = this.mapEngine.init([75.10632, 12.30814], 12);

        // 2. Instantiate modular sub-systems
        this.globeManager = new GlobeManager(this.mapEngine, this.token);
        this.routesManager = new RoutesManager(this.mapEngine, this.token);
        this.placesManager = new PlacesManager(this.mapEngine, this.token);
        this.flightsManager = new FlightsManager(this.mapEngine, this.token);
        this.issManager = new ISSManager(this.mapEngine, this.token);
        this.layersManager = new LayersManager(this.mapEngine, this.token);
        this.controlsManager = new ControlsManager(this.mapEngine);
        this.searchManager = new SearchManager(this.mapEngine, this.token);
        this.settingsManager = new SettingsManager(this.mapEngine, this.token);

        // 3. Wait for style to load before initialising sub-systems
        map.on('load', () => {
            this.globeManager.init();
            this.routesManager.init();
            this.placesManager.init();
            this.flightsManager.init();
            this.issManager.init();
            this.layersManager.init();
            this.controlsManager.init();
            this.searchManager.init();
            this.settingsManager.init();

            this._bindWorkspaceUI();
            this._setupRealtimeLink();
            this._setupCoordinateTracking();
        });
    }

    _bindWorkspaceUI() {
        // Tab switching in left navigation
        document.querySelectorAll('.nav-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                const targetTab = tab.dataset.tab;
                document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
                tab.classList.add('active');

                // Switch pane in drawer
                document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
                const pane = document.getElementById(`tab-${targetTab}`);
                if (pane) pane.classList.add('active');

                // Ensure drawer is open
                const drawer = document.getElementById('sidebar-drawer');
                drawer?.classList.remove('drawer-collapsed');

                // Update drawer header title
                const title = document.getElementById('drawer-title');
                if (title) title.textContent = tab.getAttribute('title').toUpperCase();
            });
        });

        // Drawer collapse toggle
        document.getElementById('drawer-toggle-btn')?.addEventListener('click', () => {
            const drawer = document.getElementById('sidebar-drawer');
            drawer?.classList.toggle('drawer-collapsed');
        });

        // Right Info panel close
        document.getElementById('info-panel-close-btn')?.addEventListener('click', () => {
            document.getElementById('info-panel')?.classList.add('info-panel-collapsed');
        });

        // 2D Map / 3D Globe projection switch buttons
        const btn2D = document.getElementById('btn-view-2d');
        const btnGlobe = document.getElementById('btn-view-globe');

        if (btn2D && btnGlobe) {
            btn2D.addEventListener('click', () => {
                btn2D.classList.add('active');
                btnGlobe.classList.remove('active');
                this.mapEngine.setProjection('mercator');
            });

            btnGlobe.addEventListener('click', () => {
                btnGlobe.classList.add('active');
                btn2D.classList.remove('active');
                this.mapEngine.setProjection('globe');
            });
        }

        // Current Location (HTML5 Geolocation)
        document.getElementById('btn-locate-me')?.addEventListener('click', () => {
            if ('geolocation' in navigator) {
                navigator.geolocation.getCurrentPosition(
                    (pos) => {
                        const { latitude, longitude } = pos.coords;
                        this.mapEngine.flyTo(longitude, latitude, 14);
                        this.mapEngine.addMarker(longitude, latitude, {
                            color: '#00e676',
                            popupHtml: '<b>Current Location</b><br>Your detected geographic position.'
                        });
                    },
                    (err) => {
                        alert('Could not determine current location: ' + err.message);
                    },
                    { enableHighAccuracy: true, timeout: 8000 }
                );
            } else {
                alert('Geolocation API not supported in this browser.');
            }
        });
    }

    _setupCoordinateTracking() {
        this.mapEngine.onMouseMove((info) => {
            const lat = info.lat;
            const lng = info.lng;

            // Decimal display
            const decEl = document.getElementById('coord-dec');
            if (decEl) {
                const latStr = Math.abs(lat).toFixed(5) + '° ' + (lat >= 0 ? 'N' : 'S');
                const lngStr = Math.abs(lng).toFixed(5) + '° ' + (lng >= 0 ? 'E' : 'W');
                decEl.textContent = `${latStr}, ${lngStr}`;
            }

            // DMS display
            const dmsEl = document.getElementById('coord-dms');
            if (dmsEl) {
                dmsEl.textContent = this._toDMS(lat, lng);
            }

            // HUD Zoom & Pitch
            const zoomEl = document.getElementById('hud-zoom');
            if (zoomEl) zoomEl.textContent = info.zoom.toFixed(1);
            const pitchEl = document.getElementById('hud-pitch');
            if (pitchEl) pitchEl.textContent = `${Math.round(info.pitch)}°`;
        });
    }

    _toDMS(lat, lon) {
        const toDMSStr = (val, isLat) => {
            const dir = isLat ? (val >= 0 ? 'N' : 'S') : (val >= 0 ? 'E' : 'W');
            const abs = Math.abs(val);
            const d = Math.floor(abs);
            const rem = (abs - d) * 60;
            const m = Math.floor(rem);
            const s = Math.round((rem - m) * 60);
            return `${d}° ${m.toString().padStart(2, '0')}' ${s.toString().padStart(2, '0')}" ${dir}`;
        };
        return `${toDMSStr(lat, true)}, ${toDMSStr(lon, false)}`;
    }

    _setupRealtimeLink() {
        const sseUrl = `/api/events?token=${encodeURIComponent(this.token)}`;
        try {
            this.eventSource = new EventSource(sseUrl);

            this.eventSource.onopen = () => {
                const ind = document.getElementById('connection-indicator');
                const lbl = document.getElementById('connection-label');
                const diag = document.getElementById('diag-sse-status');
                if (ind) ind.className = 'status-indicator online';
                if (lbl) lbl.textContent = 'JARVIS LINK: LIVE SSE';
                if (diag) diag.textContent = 'Connected (Active SSE)';
            };

            this.eventSource.onmessage = (event) => {
                try {
                    const payload = JSON.parse(event.data);
                    if (payload.type === 'command') {
                        this.handleRemoteVoiceCommand(payload.action, payload.data);
                    }
                } catch (e) {
                    console.warn('[Realtime Link] Parse error:', e);
                }
            };

            this.eventSource.onerror = () => {
                const ind = document.getElementById('connection-indicator');
                const lbl = document.getElementById('connection-label');
                const diag = document.getElementById('diag-sse-status');
                if (ind) ind.className = 'status-indicator offline';
                if (lbl) lbl.textContent = 'JARVIS LINK: RECONNECTING';
                if (diag) diag.textContent = 'Polling fallback';

                // Fallback polling
                setTimeout(() => this._pollCommandsFallback(), 3000);
            };
        } catch (e) {
            console.warn('[Realtime Link] SSE not available, using poll fallback:', e);
            setInterval(() => this._pollCommandsFallback(), 3000);
        }
    }

    async _pollCommandsFallback() {
        try {
            const resp = await fetch(`/api/poll?token=${encodeURIComponent(this.token)}`);
            if (resp.ok) {
                const data = await resp.json();
                const commands = data.commands || [];
                commands.forEach(cmd => {
                    if (cmd.action && (!cmd._handled)) {
                        cmd._handled = true;
                        this.handleRemoteVoiceCommand(cmd.action, cmd.data);
                    }
                });
            }
        } catch (e) {}
    }

    /** Dispatch remote voice commands received from JARVIS Python plugin */
    handleRemoteVoiceCommand(action, data) {
        console.log(`[JARVIS Voice Command] Action: ${action}`, data);

        if (action === 'fly_to') {
            this.mapEngine.flyTo(data.longitude, data.latitude, data.zoom || 13);
            this.mapEngine.clearMarkers();
            this.mapEngine.addMarker(data.longitude, data.latitude, {
                popupHtml: `<b>${data.name || 'Location'}</b><br>${data.display_name || ''}`
            });
        }
        else if (action === 'set_projection') {
            const proj = data.projection || 'mercator';
            const btn2D = document.getElementById('btn-view-2d');
            const btnGlobe = document.getElementById('btn-view-globe');
            if (proj === 'globe') {
                btnGlobe?.classList.add('active');
                btn2D?.classList.remove('active');
            } else {
                btn2D?.classList.add('active');
                btnGlobe?.classList.remove('active');
            }
            this.mapEngine.setProjection(proj);
        }
        else if (action === 'set_layer') {
            const layer = data.layer || 'streets';
            this.mapEngine.setStyle(layer);
            const quickSelect = document.getElementById('layer-quick-select');
            if (quickSelect) quickSelect.value = layer;
        }
        else if (action === 'show_route') {
            this.routesManager.displayRoute(data);
            document.querySelector('.nav-tab[data-tab="routes"]')?.click();
        }
        else if (action === 'clear_route') {
            this.routesManager.clearRoute();
        }
        else if (action === 'track_aircraft') {
            document.querySelector('.nav-tab[data-tab="flights"]')?.click();
            if (data.callsign || data.icao24) {
                this.flightsManager.searchFlight(data.callsign || data.icao24);
            }
        }
        else if (action === 'enable_flight_radar') {
            document.querySelector('.nav-tab[data-tab="flights"]')?.click();
        }
        else if (action === 'track_iss') {
            document.querySelector('.nav-tab[data-tab="iss"]')?.click();
            if (this.issManager.currentTelemetry) {
                const t = this.issManager.currentTelemetry;
                this.mapEngine.flyTo(t.longitude, t.latitude, 4);
            }
        }
        else if (action === 'show_destinations') {
            document.querySelector('.nav-tab[data-tab="destinations"]')?.click();
            if (data.category) {
                this.placesManager.loadDestinations(data.category);
            }
        }
        else if (action === 'show_places') {
            document.querySelector('.nav-tab[data-tab="places"]')?.click();
            if (data.places) {
                this.placesManager.renderPlacesList(data.places, data.category || 'places');
            }
        }
        else if (action === 'close_session') {
            const banner = document.getElementById('measure-banner');
            const text = document.getElementById('measure-text');
            if (banner && text) {
                banner.classList.remove('hidden');
                text.textContent = 'Map session flagged closed by JARVIS. You may close this tab.';
            }
        }
    }
}

// Instantiate on DOMContentLoaded
window.addEventListener('DOMContentLoaded', () => {
    const app = new MapExplorerApp();
    app.init();
    window.jarvisMapApp = app;
});
