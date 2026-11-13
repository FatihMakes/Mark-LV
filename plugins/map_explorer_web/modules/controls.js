/**
 * Interactive Controls & Spatial Measurement module for JARVIS Map Explorer.
 * Provides distance measuring, polygon area calculation, compass reset,
 * 3D perspective pitch controls, and coordinate copying.
 */

export class ControlsManager {
    constructor(mapEngine) {
        this.engine = mapEngine;
        this.measureMode = null; // 'distance' | 'area' | null
        this.measurePoints = [];
        this.measureMarkers = [];
    }

    init() {
        this._bindControls();
        this._initMeasureLayers();
    }

    _bindControls() {
        // Compass reset North
        const compassBtn = document.getElementById('ctrl-compass');
        if (compassBtn) {
            compassBtn.addEventListener('click', () => {
                this.engine.map?.easeTo({ bearing: 0, pitch: 0, duration: 800 });
            });
        }

        // Zoom In / Out
        document.getElementById('ctrl-zoom-in')?.addEventListener('click', () => {
            this.engine.map?.zoomIn({ duration: 400 });
        });
        document.getElementById('ctrl-zoom-out')?.addEventListener('click', () => {
            this.engine.map?.zoomOut({ duration: 400 });
        });

        // 3D Pitch toggle (0 -> 35 -> 60 -> 0)
        document.getElementById('ctrl-pitch-toggle')?.addEventListener('click', () => {
            const map = this.engine.map;
            if (!map) return;
            const currentPitch = map.getPitch();
            let nextPitch = 0;
            if (currentPitch < 20) nextPitch = 45;
            else if (currentPitch < 55) nextPitch = 65;
            else nextPitch = 0;
            map.easeTo({ pitch: nextPitch, duration: 600 });
        });

        // Measure Distance Tool
        document.getElementById('ctrl-measure-dist')?.addEventListener('click', () => {
            this.toggleMeasure('distance');
        });

        // Measure Area Tool
        document.getElementById('ctrl-measure-area')?.addEventListener('click', () => {
            this.toggleMeasure('area');
        });

        // Clear measurement banner button
        document.getElementById('measure-clear-btn')?.addEventListener('click', () => {
            this.clearMeasurement();
        });

        // Coordinate display click to copy
        document.getElementById('coords-display')?.addEventListener('click', () => {
            const dec = document.getElementById('coord-dec')?.textContent || '';
            const dms = document.getElementById('coord-dms')?.textContent || '';
            const textToCopy = `${dec} (${dms})`;
            navigator.clipboard.writeText(textToCopy).then(() => {
                const label = document.getElementById('connection-label');
                if (label) {
                    const prev = label.textContent;
                    label.textContent = 'COORDINATES COPIED TO CLIPBOARD';
                    setTimeout(() => { label.textContent = prev; }, 2000);
                }
            }).catch(() => {});
        });

        // Fullscreen toggle
        document.getElementById('btn-fullscreen')?.addEventListener('click', () => {
            if (!document.fullscreenElement) {
                document.documentElement.requestFullscreen().catch(() => {});
            } else {
                document.exitFullscreen().catch(() => {});
            }
        });

        // Map Click for measurement points
        this.engine.onClick((e) => {
            if (!this.measureMode) return;
            this.addMeasurePoint(e.lng, e.lat);
        });

        // Update compass needle rotation on map rotate
        this.engine.map?.on('rotate', () => {
            const bearing = this.engine.map.getBearing();
            const needle = document.getElementById('compass-needle');
            if (needle) {
                needle.style.transform = `rotate(${-bearing}deg)`;
            }
        });
    }

    _initMeasureLayers() {
        const map = this.engine.map;
        if (!map) return;
        if (!map.isStyleLoaded()) {
            map.once('style.load', () => this._initMeasureLayers());
            return;
        }

        if (!map.getSource('measure-source')) {
            map.addSource('measure-source', {
                type: 'geojson',
                data: { type: 'FeatureCollection', features: [] }
            });

            // Area fill layer
            map.addLayer({
                id: 'measure-area-fill',
                type: 'fill',
                source: 'measure-source',
                paint: {
                    'fill-color': '#00e5ff',
                    'fill-opacity': 0.25
                }
            });

            // Distance line layer
            map.addLayer({
                id: 'measure-dist-line',
                type: 'line',
                source: 'measure-source',
                paint: {
                    'line-color': '#00e5ff',
                    'line-width': 2.5,
                    'line-dasharray': [2, 1]
                }
            });
        }
    }

    toggleMeasure(mode) {
        if (this.measureMode === mode) {
            this.clearMeasurement();
            return;
        }

        this.clearMeasurement();
        this.measureMode = mode;

        const banner = document.getElementById('measure-banner');
        const text = document.getElementById('measure-text');
        if (banner && text) {
            banner.classList.remove('hidden');
            text.textContent = mode === 'distance'
                ? 'Click on the map to add distance waypoints.'
                : 'Click 3 or more points on the map to measure area.';
        }
    }

    addMeasurePoint(lng, lat) {
        this.measurePoints.push([lng, lat]);
        const map = this.engine.map;
        if (!map) return;

        // Place point marker
        const el = document.createElement('div');
        el.style.width = '10px';
        el.style.height = '10px';
        el.style.borderRadius = '50%';
        el.style.background = '#00e5ff';
        el.style.border = '2px solid #fff';

        const marker = new window.maplibregl.Marker({ element: el })
            .setLngLat([lng, lat])
            .addTo(map);
        this.measureMarkers.push(marker);

        // Update GeoJSON and banner calculation
        this._updateMeasureGraphics();
    }

    _updateMeasureGraphics() {
        const map = this.engine.map;
        const src = map?.getSource('measure-source');
        if (!src) return;

        if (this.measureMode === 'distance') {
            if (this.measurePoints.length >= 2) {
                src.setData({
                    type: 'Feature',
                    geometry: {
                        type: 'LineString',
                        coordinates: this.measurePoints
                    }
                });

                let totalDistM = 0;
                for (let i = 1; i < this.measurePoints.length; i++) {
                    totalDistM += this._haversine(
                        this.measurePoints[i - 1][1], this.measurePoints[i - 1][0],
                        this.measurePoints[i][1], this.measurePoints[i][0]
                    );
                }

                const distKm = (totalDistM / 1000).toFixed(2);
                const distMi = (totalDistM * 0.000621371).toFixed(2);
                document.getElementById('measure-text').textContent =
                    `Distance: ${distKm} km (${distMi} mi) | ${this.measurePoints.length} points`;
            }
        } else if (this.measureMode === 'area') {
            if (this.measurePoints.length >= 3) {
                const polygonCoords = [...this.measurePoints, this.measurePoints[0]];
                src.setData({
                    type: 'Feature',
                    geometry: {
                        type: 'Polygon',
                        coordinates: [polygonCoords]
                    }
                });

                const areaSqM = this._computePolygonArea(this.measurePoints);
                const areaSqKm = (areaSqM / 1000000).toFixed(3);
                const acres = (areaSqM * 0.000247105).toFixed(1);
                document.getElementById('measure-text').textContent =
                    `Area: ${areaSqKm} km² (${acres} acres) | ${this.measurePoints.length} vertices`;
            }
        }
    }

    clearMeasurement() {
        this.measureMode = null;
        this.measurePoints = [];
        this.measureMarkers.forEach(m => m.remove());
        this.measureMarkers = [];

        const map = this.engine.map;
        const src = map?.getSource('measure-source');
        if (src) {
            src.setData({ type: 'FeatureCollection', features: [] });
        }
        document.getElementById('measure-banner')?.classList.add('hidden');
    }

    _haversine(lat1, lon1, lat2, lon2) {
        const R = 6371000;
        const toRad = deg => (deg * Math.PI) / 180;
        const dLat = toRad(lat2 - lat1);
        const dLon = toRad(lon2 - lon1);
        const a = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * (Math.sin(dLon / 2) ** 2);
        return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    }

    _computePolygonArea(coords) {
        if (coords.length < 3) return 0;
        const R = 6378137;
        const toRad = deg => (deg * Math.PI) / 180;
        let area = 0;
        for (let i = 0; i < coords.length; i++) {
            const j = (i + 1) % coords.length;
            const xi = toRad(coords[i][0]) * Math.cos(toRad(coords[i][1])) * R;
            const yi = toRad(coords[i][1]) * R;
            const xj = toRad(coords[j][0]) * Math.cos(toRad(coords[j][1])) * R;
            const yj = toRad(coords[j][1]) * R;
            area += (xi * yj) - (xj * yi);
        }
        return Math.abs(area / 2);
    }
}
