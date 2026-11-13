/**
 * International Space Station (ISS) Tracker & Orbital Propagation module
 * for JARVIS Map Explorer.
 */

export class ISSManager {
    constructor(mapEngine, sessionToken = '') {
        this.engine = mapEngine;
        this.token = sessionToken;
        this.active = true;
        this.followCamera = false;
        this.showOrbit = true;
        this.issMarker = null;
        this.pollTimer = null;
        this.currentTelemetry = null;
    }

    init() {
        this._bindUI();
        this._ensureLayers();
        this.startPolling();
    }

    _bindUI() {
        const activeToggle = document.getElementById('iss-active-toggle');
        if (activeToggle) {
            activeToggle.addEventListener('change', () => {
                this.active = activeToggle.checked;
                if (this.active) {
                    this.startPolling();
                } else {
                    this.stopPolling();
                    if (this.issMarker) this.issMarker.remove();
                    this.issMarker = null;
                }
            });
        }

        const followToggle = document.getElementById('iss-follow-toggle');
        if (followToggle) {
            followToggle.addEventListener('change', () => {
                this.followCamera = followToggle.checked;
                if (this.followCamera && this.currentTelemetry) {
                    this.engine.flyTo(this.currentTelemetry.longitude, this.currentTelemetry.latitude, 4);
                }
            });
        }

        const focusBtn = document.getElementById('iss-focus-btn');
        if (focusBtn) {
            focusBtn.addEventListener('click', () => {
                if (this.currentTelemetry) {
                    this.engine.flyTo(this.currentTelemetry.longitude, this.currentTelemetry.latitude, 4);
                }
            });
        }

        const orbitBtn = document.getElementById('iss-orbit-toggle-btn');
        if (orbitBtn) {
            orbitBtn.addEventListener('click', () => {
                this.showOrbit = !this.showOrbit;
                const map = this.engine.map;
                if (map && map.getLayer('iss-predicted-orbit-line')) {
                    map.setLayoutProperty('iss-predicted-orbit-line', 'visibility', this.showOrbit ? 'visible' : 'none');
                }
            });
        }
    }

    _ensureLayers() {
        const map = this.engine.map;
        if (!map) return;
        if (!map.isStyleLoaded()) {
            map.once('style.load', () => this._ensureLayers());
            return;
        }

        // Predicted orbit path layer
        if (!map.getSource('iss-orbit-source')) {
            map.addSource('iss-orbit-source', {
                type: 'geojson',
                data: { type: 'FeatureCollection', features: [] }
            });

            map.addLayer({
                id: 'iss-predicted-orbit-line',
                type: 'line',
                source: 'iss-orbit-source',
                paint: {
                    'line-color': '#00e5ff',
                    'line-width': 2,
                    'line-dasharray': [4, 3],
                    'line-opacity': 0.75
                }
            });
        }

        // Historical ground track layer
        if (!map.getSource('iss-history-source')) {
            map.addSource('iss-history-source', {
                type: 'geojson',
                data: { type: 'FeatureCollection', features: [] }
            });

            map.addLayer({
                id: 'iss-history-line',
                type: 'line',
                source: 'iss-history-source',
                paint: {
                    'line-color': '#ffab00',
                    'line-width': 2.5,
                    'line-opacity': 0.8
                }
            });
        }
    }

    startPolling() {
        if (this.pollTimer) clearInterval(this.pollTimer);
        this.fetchTelemetry();
        this.pollTimer = setInterval(() => {
            if (this.active) this.fetchTelemetry();
        }, 2500); // 2.5 second polling
    }

    stopPolling() {
        if (this.pollTimer) {
            clearInterval(this.pollTimer);
            this.pollTimer = null;
        }
    }

    async fetchTelemetry() {
        try {
            const resp = await fetch(`/api/iss?token=${encodeURIComponent(this.token)}`);
            const data = await resp.json();
            this.currentTelemetry = data;

            this.updateMarker(data);
            this.updateHUD(data);
            this.updateHistoryTrack(data.history || []);

            // Periodic orbit recalculation (~every 60s)
            if (!this.lastOrbitUpdate || Date.now() - this.lastOrbitUpdate > 60000) {
                this.updatePredictedOrbit(data.latitude, data.longitude);
                this.lastOrbitUpdate = Date.now();
            }

            if (this.followCamera) {
                this.engine.map?.panTo([data.longitude, data.latitude], { duration: 2000 });
            }
        } catch (e) {
            console.warn('[ISSManager] Telemetry fetch error:', e);
        }
    }

    updateMarker(data) {
        const map = this.engine.map;
        if (!map || !window.maplibregl) return;

        if (!this.issMarker) {
            const el = document.createElement('div');
            el.className = 'iss-marker';
            el.innerHTML = `
                <div class="iss-pulse-circle"></div>
                <div class="iss-icon-inner">🛰️</div>
            `;

            this.issMarker = new window.maplibregl.Marker({ element: el })
                .setLngLat([data.longitude, data.latitude])
                .addTo(map);

            el.addEventListener('click', () => {
                this.showISSSummary(data);
            });
        } else {
            this.issMarker.setLngLat([data.longitude, data.latitude]);
        }
    }

    updateHUD(data) {
        document.getElementById('iss-lat-val').textContent = `${data.latitude.toFixed(4)}°`;
        document.getElementById('iss-lon-val').textContent = `${data.longitude.toFixed(4)}°`;
        document.getElementById('iss-altitude-val').textContent = `${data.altitude_km} km`;
        document.getElementById('iss-velocity-val').textContent = `${data.velocity_kmh.toLocaleString()} km/h`;
        document.getElementById('iss-visibility-val').textContent = (data.visibility || 'daylight').toUpperCase();
        document.getElementById('iss-footprint-val').textContent = `${data.footprint_km.toLocaleString()} km`;
    }

    updateHistoryTrack(history) {
        const map = this.engine.map;
        if (!map || !history || history.length < 2) return;
        const src = map.getSource('iss-history-source');
        if (!src) return;

        const coords = history.map(h => [h.longitude, h.latitude]);
        src.setData({
            type: 'Feature',
            geometry: {
                type: 'LineString',
                coordinates: coords
            }
        });
    }

    async updatePredictedOrbit(lat, lon) {
        const map = this.engine.map;
        if (!map) return;

        try {
            const resp = await fetch(`/api/iss/orbit?lat=${lat}&lon=${lon}&minutes=100&token=${encodeURIComponent(this.token)}`);
            const data = await resp.json();
            const src = map.getSource('iss-orbit-source');
            if (src && data.features) {
                src.setData(data);
            }
        } catch (e) {
            console.warn('[ISSManager] Orbit prediction fetch error:', e);
        }
    }

    showISSSummary(data) {
        const panel = document.getElementById('info-panel');
        const title = document.getElementById('info-panel-title');
        const body = document.getElementById('info-panel-body');
        if (!panel || !body) return;

        panel.classList.remove('info-panel-collapsed');
        if (title) title.textContent = 'ISS TELEMETRY & ORBIT';

        body.innerHTML = `
            <div class="section-block">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <h3 style="color:#ffffff;font-size:16px;">INTERNATIONAL SPACE STATION</h3>
                    <span class="card-badge" style="background:#00e5ff;color:#000;font-weight:800;">NORAD 25544</span>
                </div>
                <p style="color:#78909c;font-size:11.5px;margin-top:6px;">Habitable artificial satellite in low Earth orbit at an orbital speed of ~7.66 km/s.</p>
            </div>

            <div class="telemetry-box" style="margin-bottom:14px;">
                <div class="telemetry-row">
                    <span class="t-key">Latitude:</span>
                    <span class="t-val">${data.latitude}°</span>
                </div>
                <div class="telemetry-row">
                    <span class="t-key">Longitude:</span>
                    <span class="t-val">${data.longitude}°</span>
                </div>
                <div class="telemetry-row">
                    <span class="t-key">Altitude:</span>
                    <span class="t-val">${data.altitude_km} km (${data.altitude_miles} mi)</span>
                </div>
                <div class="telemetry-row">
                    <span class="t-key">Velocity:</span>
                    <span class="t-val">${data.velocity_kmh.toLocaleString()} km/h (${data.velocity_mph.toLocaleString()} mph)</span>
                </div>
                <div class="telemetry-row">
                    <span class="t-key">Lighting Condition:</span>
                    <span class="t-val">${(data.visibility || 'daylight').toUpperCase()}</span>
                </div>
                <div class="telemetry-row">
                    <span class="t-key">Ground Footprint:</span>
                    <span class="t-val">${data.footprint_km} km</span>
                </div>
                <div class="telemetry-row">
                    <span class="t-key">Orbital Period:</span>
                    <span class="t-val">92.9 minutes</span>
                </div>
                <div class="telemetry-row">
                    <span class="t-key">Telemetry Provider:</span>
                    <span class="t-val">WhereTheISS.at</span>
                </div>
            </div>

            <div class="btn-group-row">
                <button id="iss-center-btn" class="btn-primary">CENTER ON ISS</button>
            </div>
        `;

        document.getElementById('iss-center-btn')?.addEventListener('click', () => {
            this.engine.flyTo(data.longitude, data.latitude, 4);
        });
    }
}
