/**
 * Route Planning & Navigation module for JARVIS Map Explorer.
 * Calculates road routes with turn-by-turn guidance and elevation profiles.
 */

export class RoutesManager {
    constructor(mapEngine, sessionToken = '') {
        this.engine = mapEngine;
        this.token = sessionToken;
        this.activeRoute = null;
        this.travelMode = 'driving';
        this.waypointMarkers = [];
    }

    init() {
        this._bindUI();
        this._ensureRouteLayers();
    }

    _bindUI() {
        document.querySelectorAll('.mode-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.travelMode = btn.dataset.mode || 'driving';
            });
        });

        const calcBtn = document.getElementById('calc-route-btn');
        if (calcBtn) calcBtn.addEventListener('click', () => this.calculateFromInputs());

        const clearBtn = document.getElementById('clear-route-btn');
        if (clearBtn) clearBtn.addEventListener('click', () => this.clearRoute());

        const swapBtn = document.getElementById('swap-route-btn');
        if (swapBtn) {
            swapBtn.addEventListener('click', () => {
                const orig = document.getElementById('route-origin');
                const dest = document.getElementById('route-destination');
                if (orig && dest) {
                    const tmp = orig.value;
                    orig.value = dest.value;
                    dest.value = tmp;
                }
            });
        }
    }

    _ensureRouteLayers() {
        const map = this.engine.map;
        if (!map) return;
        if (!map.isStyleLoaded()) {
            map.once('style.load', () => this._ensureRouteLayers());
            return;
        }

        if (!map.getSource('route-source')) {
            map.addSource('route-source', {
                type: 'geojson',
                data: { type: 'FeatureCollection', features: [] }
            });

            map.addLayer({
                id: 'route-line-casing',
                type: 'line',
                source: 'route-source',
                layout: { 'line-join': 'round', 'line-cap': 'round' },
                paint: {
                    'line-color': '#00a8ff',
                    'line-width': 8,
                    'line-opacity': 0.4,
                    'line-blur': 3
                }
            });

            map.addLayer({
                id: 'route-line',
                type: 'line',
                source: 'route-source',
                layout: { 'line-join': 'round', 'line-cap': 'round' },
                paint: {
                    'line-color': '#00e5ff',
                    'line-width': 4,
                    'line-opacity': 0.95
                }
            });
        }
    }

    async calculateFromInputs() {
        const origInput = document.getElementById('route-origin')?.value.trim();
        const destInput = document.getElementById('route-destination')?.value.trim();
        if (!origInput || !destInput) {
            alert('Please specify both Origin and Destination.');
            return;
        }

        try {
            const origRes = await fetch('/api/geocode?q=' + encodeURIComponent(origInput) + '&limit=1&token=' + encodeURIComponent(this.token));
            const origData = await origRes.json();
            if (!origData.results || !origData.results.length) {
                alert('Origin not found: ' + origInput);
                return;
            }

            const destRes = await fetch('/api/geocode?q=' + encodeURIComponent(destInput) + '&limit=1&token=' + encodeURIComponent(this.token));
            const destData = await destRes.json();
            if (!destData.results || !destData.results.length) {
                alert('Destination not found: ' + destInput);
                return;
            }

            const p1 = origData.results[0];
            const p2 = destData.results[0];

            await this.calculateRoute([
                [p1.longitude, p1.latitude],
                [p2.longitude, p2.latitude]
            ], this.travelMode, p1.name, p2.name);
        } catch (e) {
            alert('Routing calculation error: ' + e.message);
        }
    }

    async calculateRoute(coordinates, mode = 'driving', origName = 'Origin', destName = 'Destination') {
        this._ensureRouteLayers();
        try {
            const resp = await fetch('/api/routes?token=' + encodeURIComponent(this.token), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    coordinates: coordinates,
                    travel_mode: mode,
                    provider: 'osrm'
                })
            });

            const result = await resp.json();
            if (result.success) {
                this.displayRoute(result, origName, destName);
                return result;
            } else {
                alert('Routing error: ' + (result.error || 'No route found'));
            }
        } catch (e) {
            console.error('[RoutesManager]', e);
        }
    }

    displayRoute(routeData, origName = '', destName = '') {
        this.activeRoute = routeData;
        const map = this.engine.map;
        if (!map) return;
        this._ensureRouteLayers();

        const src = map.getSource('route-source');
        if (src) {
            src.setData({
                type: 'Feature',
                geometry: routeData.geometry
            });
        }

        this.waypointMarkers.forEach(m => m.remove());
        this.waypointMarkers = [];

        const coords = routeData.geometry.coordinates;
        if (coords && coords.length > 0) {
            const startPt = coords[0];
            const endPt = coords[coords.length - 1];

            const startEl = document.createElement('div');
            startEl.className = 'waypoint-bullet start';
            startEl.textContent = 'A';
            const m1 = new window.maplibregl.Marker({ element: startEl })
                .setLngLat(startPt)
                .setPopup(new window.maplibregl.Popup().setHTML('<b>Origin:</b> ' + (origName || 'Start')))
                .addTo(map);

            const endEl = document.createElement('div');
            endEl.className = 'waypoint-bullet end';
            endEl.textContent = 'B';
            const m2 = new window.maplibregl.Marker({ element: endEl })
                .setLngLat(endPt)
                .setPopup(new window.maplibregl.Popup().setHTML('<b>Destination:</b> ' + (destName || 'Finish')))
                .addTo(map);

            this.waypointMarkers.push(m1, m2);

            if (routeData.bounds) {
                map.fitBounds(routeData.bounds, { padding: 80, duration: 1500 });
            }
        }

        const summaryCard = document.getElementById('route-summary-card');
        if (summaryCard) {
            summaryCard.classList.remove('hidden');
            document.getElementById('route-distance').textContent = routeData.distance_km + ' km';
            document.getElementById('route-duration').textContent = routeData.duration_text;
            document.getElementById('route-provider-badge').textContent = (routeData.provider || 'OSRM').split(' ')[0];

            const turnsList = document.getElementById('turn-instructions-list');
            turnsList.innerHTML = '';
            (routeData.turn_by_turn || []).forEach(step => {
                const li = document.createElement('li');
                li.innerHTML = '<div>' + step.instruction + '</div><span class="turn-dist">' + step.distance_m + ' m (' + step.duration_s + 's)</span>';
                turnsList.appendChild(li);
            });

            if (routeData.elevation_profile && routeData.elevation_profile.length > 0) {
                this._drawElevationProfile(routeData.elevation_profile);
            }
        }
    }

    clearRoute() {
        this.activeRoute = null;
        const map = this.engine.map;
        if (map && map.getSource('route-source')) {
            map.getSource('route-source').setData({ type: 'FeatureCollection', features: [] });
        }
        this.waypointMarkers.forEach(m => m.remove());
        this.waypointMarkers = [];
        document.getElementById('route-summary-card')?.classList.add('hidden');
    }

    _drawElevationProfile(profile) {
        const canvas = document.getElementById('elevation-canvas');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const w = canvas.width;
        const h = canvas.height;
        ctx.clearRect(0, 0, w, h);

        const elevs = profile.map(p => p.elevation_meters || 0);
        const minElev = Math.min(...elevs);
        const maxElev = Math.max(...elevs);
        const range = Math.max(10, maxElev - minElev);

        ctx.strokeStyle = 'rgba(0, 229, 255, 0.15)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(0, h / 2);
        ctx.lineTo(w, h / 2);
        ctx.stroke();

        const grad = ctx.createLinearGradient(0, 0, 0, h);
        grad.addColorStop(0, 'rgba(0, 229, 255, 0.5)');
        grad.addColorStop(1, 'rgba(0, 229, 255, 0.02)');

        ctx.beginPath();
        profile.forEach((pt, idx) => {
            const x = (idx / (profile.length - 1)) * w;
            const y = h - ((pt.elevation_meters - minElev) / range) * (h - 16) - 8;
            if (idx === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        });
        ctx.lineTo(w, h);
        ctx.lineTo(0, h);
        ctx.closePath();
        ctx.fillStyle = grad;
        ctx.fill();

        ctx.strokeStyle = '#00e5ff';
        ctx.lineWidth = 2;
        ctx.beginPath();
        profile.forEach((pt, idx) => {
            const x = (idx / (profile.length - 1)) * w;
            const y = h - ((pt.elevation_meters - minElev) / range) * (h - 16) - 8;
            if (idx === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        });
        ctx.stroke();
    }
}
