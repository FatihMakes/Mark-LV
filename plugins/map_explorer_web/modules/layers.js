/**
 * Layers & Overlays Manager for JARVIS Map Explorer.
 * Controls base map styles, USGS live earthquakes, and atmospheric overlays.
 */

export class LayersManager {
    constructor(mapEngine, sessionToken = '') {
        this.engine = mapEngine;
        this.token = sessionToken;
        this.earthquakesActive = true;
        this.weatherTimer = null;
    }

    init() {
        this._bindUI();
        this._initEarthquakesLayer();
        this.updateWeather();

        // Refresh weather periodically on pan
        this.engine.map?.on('moveend', () => {
            clearTimeout(this.weatherTimer);
            this.weatherTimer = setTimeout(() => this.updateWeather(), 2000);
        });
    }

    _bindUI() {
        // Base layer radio changes
        document.querySelectorAll('input[name="base-layer"]').forEach(radio => {
            radio.addEventListener('change', () => {
                if (radio.checked) {
                    this.engine.setStyle(radio.value);
                    const quickSelect = document.getElementById('layer-quick-select');
                    if (quickSelect) quickSelect.value = radio.value;
                }
            });
        });

        // Quick select in top bar
        const quickSelect = document.getElementById('layer-quick-select');
        if (quickSelect) {
            quickSelect.addEventListener('change', () => {
                const val = quickSelect.value;
                this.engine.setStyle(val);
                const radio = document.querySelector(`input[name="base-layer"][value="${val}"]`);
                if (radio) radio.checked = true;
            });
        }

        // Earthquakes toggle
        const eqToggle = document.getElementById('overlay-earthquakes');
        if (eqToggle) {
            eqToggle.addEventListener('change', () => {
                this.earthquakesActive = eqToggle.checked;
                const map = this.engine.map;
                if (map && map.getLayer('earthquake-circles')) {
                    map.setLayoutProperty('earthquake-circles', 'visibility', this.earthquakesActive ? 'visible' : 'none');
                }
            });
        }
    }

    _initEarthquakesLayer() {
        const map = this.engine.map;
        if (!map) return;
        if (!map.isStyleLoaded()) {
            map.once('style.load', () => this._initEarthquakesLayer());
            return;
        }

        if (!map.getSource('earthquake-source')) {
            map.addSource('earthquake-source', {
                type: 'geojson',
                data: { type: 'FeatureCollection', features: [] }
            });

            map.addLayer({
                id: 'earthquake-circles',
                type: 'circle',
                source: 'earthquake-source',
                paint: {
                    'circle-radius': [
                        'interpolate', ['linear'], ['get', 'mag'],
                        2.5, 4,
                        5.0, 9,
                        7.0, 16
                    ],
                    'circle-color': [
                        'interpolate', ['linear'], ['get', 'mag'],
                        2.5, '#00e676',
                        4.5, '#ffab00',
                        6.0, '#ff1744'
                    ],
                    'circle-opacity': 0.75,
                    'circle-stroke-width': 1.5,
                    'circle-stroke-color': '#ffffff'
                }
            });

            map.on('click', 'earthquake-circles', (e) => {
                if (!e.features || !e.features.length) return;
                const feat = e.features[0];
                const props = feat.properties;
                const geom = feat.geometry;
                this._showEarthquakeDetails(props, geom.coordinates);
            });

            this.fetchEarthquakes();
        }
    }

    async fetchEarthquakes() {
        const map = this.engine.map;
        if (!map) return;

        try {
            const resp = await fetch(`/api/layers/earthquakes?feed=2.5_day&token=${encodeURIComponent(this.token)}`);
            const data = await resp.json();
            const src = map.getSource('earthquake-source');
            if (src && data.features) {
                src.setData(data);
            }
        } catch (e) {
            console.warn('[LayersManager] Earthquakes fetch error:', e);
        }
    }

    _showEarthquakeDetails(props, coords) {
        const panel = document.getElementById('info-panel');
        const title = document.getElementById('info-panel-title');
        const body = document.getElementById('info-panel-body');
        if (!panel || !body) return;

        panel.classList.remove('info-panel-collapsed');
        if (title) title.textContent = 'SEISMIC INTELLIGENCE';

        const mag = props.mag || 'N/A';
        const place = props.place || 'Unknown Location';
        const timeStr = props.time ? new Date(Number(props.time)).toUTCString() : 'Recent';

        body.innerHTML = `
            <div class="section-block">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <h3 style="color:#ffffff;font-size:16px;">EARTHQUAKE M${mag}</h3>
                    <span class="card-badge" style="background:#ff1744;color:#fff;font-weight:800;">USGS</span>
                </div>
                <p style="color:#00e5ff;font-size:12px;margin-top:6px;">${place}</p>
            </div>

            <div class="telemetry-box" style="margin-bottom:14px;">
                <div class="telemetry-row">
                    <span class="t-key">Magnitude:</span>
                    <span class="t-val">${mag} Richter</span>
                </div>
                <div class="telemetry-row">
                    <span class="t-key">Coordinates:</span>
                    <span class="t-val">${coords[1].toFixed(4)}, ${coords[0].toFixed(4)}</span>
                </div>
                <div class="telemetry-row">
                    <span class="t-key">Depth:</span>
                    <span class="t-val">${coords[2] || 10} km</span>
                </div>
                <div class="telemetry-row">
                    <span class="t-key">Timestamp:</span>
                    <span class="t-val">${timeStr}</span>
                </div>
                <div class="telemetry-row">
                    <span class="t-key">Source:</span>
                    <span class="t-val">USGS Earthquake Hazards Program</span>
                </div>
            </div>
        `;
    }

    async updateWeather() {
        const map = this.engine.map;
        if (!map) return;
        const center = map.getCenter();

        try {
            const resp = await fetch(`/api/layers/weather?lat=${center.lat}&lon=${center.lng}&token=${encodeURIComponent(this.token)}`);
            const data = await resp.json();
            const badge = document.getElementById('weather-quick-badge');
            if (badge && data.live) {
                badge.textContent = `WEATHER: ${data.temperature_c}°C ${data.condition}`;
            }
        } catch (e) {
            // Weather service fallback
        }
    }
}
