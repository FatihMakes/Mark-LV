/**
 * Live Flight Radar module for JARVIS Map Explorer.
 * Integrates OpenSky Network ADS-B telemetry, heading-oriented aircraft markers,
 * viewport queries, and historical track rendering.
 */

export class FlightsManager {
    constructor(mapEngine, sessionToken = '') {
        this.engine = mapEngine;
        this.token = sessionToken;
        this.radarActive = true;
        this.aircraftMarkers = new Map(); // icao24 -> Marker
        this.aircraftData = [];
        this.selectedAircraft = null;
        this.pollTimer = null;
        this.trackLine = null;
    }

    init() {
        this._bindUI();
        this._ensureTrackLayers();
        this.startPolling();
    }

    _bindUI() {
        const toggle = document.getElementById('flight-radar-toggle');
        if (toggle) {
            toggle.addEventListener('change', () => {
                this.radarActive = toggle.checked;
                if (this.radarActive) {
                    this.pollFlights();
                    this.startPolling();
                } else {
                    this.stopPolling();
                    this.clearAircraftMarkers();
                }
            });
        }

        const searchBtn = document.getElementById('flight-search-btn');
        const searchInput = document.getElementById('flight-search-input');
        if (searchBtn && searchInput) {
            const doSearch = () => {
                const q = searchInput.value.trim().toUpperCase();
                if (!q) return;
                this.searchFlight(q);
            };
            searchBtn.addEventListener('click', doSearch);
            searchInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') doSearch();
            });
        }
    }

    _ensureTrackLayers() {
        const map = this.engine.map;
        if (!map) return;
        if (!map.isStyleLoaded()) {
            map.once('style.load', () => this._ensureTrackLayers());
            return;
        }

        if (!map.getSource('flight-track-source')) {
            map.addSource('flight-track-source', {
                type: 'geojson',
                data: { type: 'FeatureCollection', features: [] }
            });

            map.addLayer({
                id: 'flight-track-line',
                type: 'line',
                source: 'flight-track-source',
                paint: {
                    'line-color': '#ffab00',
                    'line-width': 3,
                    'line-opacity': 0.85,
                    'line-dasharray': [2, 2]
                }
            });
        }
    }

    startPolling() {
        if (this.pollTimer) clearInterval(this.pollTimer);
        this.pollFlights();
        this.pollTimer = setInterval(() => {
            if (this.radarActive) this.pollFlights();
        }, 9000); // 9 seconds interval complies with OpenSky limits
    }

    stopPolling() {
        if (this.pollTimer) {
            clearInterval(this.pollTimer);
            this.pollTimer = null;
        }
    }

    async pollFlights() {
        const map = this.engine.map;
        if (!map || !this.radarActive) return;

        const bounds = map.getBounds();
        const lamin = bounds.getSouth();
        const lomin = bounds.getWest();
        const lamax = bounds.getNorth();
        const lomax = bounds.getEast();

        // Avoid querying if bounds cover whole globe repeatedly
        const isGlobal = (lamax - lamin > 140) || (lomax - lomin > 300);
        const url = isGlobal
            ? `/api/flights?limit=150&token=${encodeURIComponent(this.token)}`
            : `/api/flights?lamin=${lamin}&lomin=${lomin}&lamax=${lamax}&lomax=${lomax}&limit=200&token=${encodeURIComponent(this.token)}`;

        try {
            const resp = await fetch(url);
            const data = await resp.json();
            const aircraft = data.aircraft || [];
            this.aircraftData = aircraft;

            // Update status counts
            const countEl = document.getElementById('radar-aircraft-count');
            if (countEl) countEl.textContent = data.count || aircraft.length;
            const freshnessEl = document.getElementById('radar-data-freshness');
            if (freshnessEl) freshnessEl.textContent = data.live ? 'Live ADS-B' : 'Cached';

            this.updateAircraftMarkers(aircraft);
            this.renderFlightsList(aircraft);
        } catch (e) {
            console.warn('[FlightsManager] OpenSky fetch error:', e);
        }
    }

    updateAircraftMarkers(aircraftList) {
        const map = this.engine.map;
        if (!map || !window.maplibregl) return;

        const currentIcaos = new Set();

        aircraftList.forEach(a => {
            currentIcaos.add(a.icao24);
            const heading = a.heading || 0;

            if (this.aircraftMarkers.has(a.icao24)) {
                // Update existing marker position and rotation
                const marker = this.aircraftMarkers.get(a.icao24);
                marker.setLngLat([a.longitude, a.latitude]);
                const iconSvg = marker.getElement().querySelector('.aircraft-icon-svg');
                if (iconSvg) {
                    iconSvg.style.transform = `rotate(${heading}deg)`;
                }
            } else {
                // Create new aircraft marker
                const el = document.createElement('div');
                el.className = 'aircraft-marker';
                el.innerHTML = `
                    <svg class="aircraft-icon-svg" style="transform: rotate(${heading}deg);" viewBox="0 0 24 24">
                        <path d="M21 16v-2l-8-5V3.5c0-.83-.67-1.5-1.5-1.5S10 2.67 10 3.5V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5l8 2.5z"/>
                    </svg>
                `;

                const marker = new window.maplibregl.Marker({ element: el })
                    .setLngLat([a.longitude, a.latitude])
                    .addTo(map);

                el.addEventListener('click', () => {
                    this.selectAircraft(a);
                });

                this.aircraftMarkers.set(a.icao24, marker);
            }
        });

        // Prune markers no longer in viewport
        for (const [icao, marker] of this.aircraftMarkers.entries()) {
            if (!currentIcaos.has(icao)) {
                marker.remove();
                this.aircraftMarkers.delete(icao);
            }
        }
    }

    renderFlightsList(aircraft) {
        const container = document.getElementById('flights-list');
        if (!container) return;
        container.innerHTML = '';

        if (aircraft.length === 0) {
            container.innerHTML = '<div class="empty-hint">No aircraft detected in current map viewport.</div>';
            return;
        }

        aircraft.slice(0, 30).forEach(a => {
            const card = document.createElement('div');
            card.className = 'flight-item-card';
            const altText = a.altitude_feet ? `${a.altitude_feet.toLocaleString()} ft` : 'Ground';
            const spdText = a.speed_knots ? `${a.speed_knots} kts` : 'N/A';

            card.innerHTML = `
                <div class="card-head">
                    <span class="card-title">${a.callsign !== 'UNTITLED' ? a.callsign : a.icao24.toUpperCase()}</span>
                    <span class="card-badge">${a.origin_country}</span>
                </div>
                <div class="card-meta">
                    Alt: ${altText} | Spd: ${spdText} | Hdg: ${a.heading}°
                </div>
            `;

            card.addEventListener('click', () => {
                this.selectAircraft(a);
                this.engine.flyTo(a.longitude, a.latitude, 9);
            });

            container.appendChild(card);
        });
    }

    async selectAircraft(a) {
        this.selectedAircraft = a;
        this.showAircraftDetails(a);

        // Fetch flight path/track
        try {
            const resp = await fetch(`/api/flights/track?icao24=${encodeURIComponent(a.icao24)}&token=${encodeURIComponent(this.token)}`);
            const trackData = await resp.json();
            if (trackData.has_track && trackData.coordinates.length > 0) {
                const map = this.engine.map;
                const src = map?.getSource('flight-track-source');
                if (src) {
                    src.setData({
                        type: 'Feature',
                        geometry: {
                            type: 'LineString',
                            coordinates: trackData.coordinates
                        }
                    });
                }
            }
        } catch (e) {
            console.warn('[FlightsManager] Track fetch error:', e);
        }
    }

    showAircraftDetails(a) {
        const panel = document.getElementById('info-panel');
        const title = document.getElementById('info-panel-title');
        const body = document.getElementById('info-panel-body');
        if (!panel || !body) return;

        panel.classList.remove('info-panel-collapsed');
        if (title) title.textContent = 'AIRCRAFT TELEMETRY';

        const altFt = a.altitude_feet ? `${a.altitude_feet.toLocaleString()} ft (${a.altitude_meters} m)` : 'On Ground';
        const spdKts = a.speed_knots ? `${a.speed_knots} kts (${a.speed_kmh} km/h)` : 'N/A';
        const vertFpm = a.vertical_rate_fpm ? `${a.vertical_rate_fpm} ft/min` : 'Level';

        body.innerHTML = `
            <div class="section-block">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <h3 style="color:#ffffff;font-size:16px;">${a.callsign}</h3>
                    <span class="card-badge" style="background:#ffab00;color:#000;font-weight:800;">ADS-B</span>
                </div>
                <div style="color:#00e5ff;font-size:11px;font-family:monospace;margin-top:2px;">ICAO24: ${a.icao24.toUpperCase()}</div>
            </div>

            <div class="telemetry-box" style="margin-bottom:14px;">
                <div class="telemetry-row">
                    <span class="t-key">Country:</span>
                    <span class="t-val">${a.origin_country}</span>
                </div>
                <div class="telemetry-row">
                    <span class="t-key">Barometric Alt:</span>
                    <span class="t-val">${altFt}</span>
                </div>
                <div class="telemetry-row">
                    <span class="t-key">Ground Speed:</span>
                    <span class="t-val">${spdKts}</span>
                </div>
                <div class="telemetry-row">
                    <span class="t-key">True Heading:</span>
                    <span class="t-val">${a.heading}°</span>
                </div>
                <div class="telemetry-row">
                    <span class="t-key">Vertical Rate:</span>
                    <span class="t-val">${vertFpm}</span>
                </div>
                <div class="telemetry-row">
                    <span class="t-key">Coordinates:</span>
                    <span class="t-val">${a.latitude}, ${a.longitude}</span>
                </div>
                ${a.squawk ? `
                <div class="telemetry-row">
                    <span class="t-key">Squawk Code:</span>
                    <span class="t-val">${a.squawk}</span>
                </div>` : ''}
                <div class="telemetry-row">
                    <span class="t-key">Provider:</span>
                    <span class="t-val">OpenSky Network</span>
                </div>
            </div>

            <div class="btn-group-row">
                <button id="flight-follow-btn" class="btn-primary">TRACK IN CENTER</button>
            </div>
        `;

        document.getElementById('flight-follow-btn')?.addEventListener('click', () => {
            this.engine.flyTo(a.longitude, a.latitude, 10);
        });
    }

    async searchFlight(query) {
        const found = this.aircraftData.find(a =>
            a.callsign.toUpperCase().includes(query) || a.icao24.toUpperCase() === query
        );

        if (found) {
            this.engine.flyTo(found.longitude, found.latitude, 10);
            this.selectAircraft(found);
        } else {
            // Query backend directly
            try {
                const resp = await fetch(`/api/flights?callsign=${encodeURIComponent(query)}&token=${encodeURIComponent(this.token)}`);
                const data = await resp.json();
                if (data.aircraft && data.aircraft.length > 0) {
                    const plane = data.aircraft[0];
                    this.engine.flyTo(plane.longitude, plane.latitude, 10);
                    this.selectAircraft(plane);
                } else {
                    alert(`No active aircraft found with callsign or ICAO: ${query}`);
                }
            } catch (e) {
                alert('Search flight query error: ' + e.message);
            }
        }
    }

    clearAircraftMarkers() {
        for (const marker of this.aircraftMarkers.values()) {
            marker.remove();
        }
        this.aircraftMarkers.clear();
    }
}
