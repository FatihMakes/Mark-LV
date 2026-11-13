/**
 * 3D Earth Globe module for JARVIS Map Explorer.
 * Adds atmospheric illumination, day/night solar terminator shadow,
 * equatorial graticule, and great-circle spherical curves.
 */

export class GlobeManager {
    constructor(mapEngine, sessionToken = '') {
        this.engine = mapEngine;
        this.token = sessionToken;
        this.terminatorVisible = true;
        this.atmosphereVisible = true;
        this.isSpinning = false;
        this.spinInterval = null;
    }

    init() {
        if (!this.engine.map) return;
        this._initLayers();
    }

    _initLayers() {
        const map = this.engine.map;
        if (!map.isStyleLoaded()) {
            map.once('style.load', () => this._initLayers());
            return;
        }

        // Add Day/Night Terminator source and layer if not present
        if (!map.getSource('terminator-source')) {
            map.addSource('terminator-source', {
                type: 'geojson',
                data: { type: 'FeatureCollection', features: [] },
            });

            map.addLayer({
                id: 'terminator-layer',
                type: 'fill',
                source: 'terminator-source',
                paint: {
                    'fill-color': '#01050c',
                    'fill-opacity': 0.45,
                },
            });

            this.updateTerminator();
        }
    }

    /** Fetch and update day/night shadow polygon */
    async updateTerminator() {
        const map = this.engine.map;
        if (!map || !map.getSource('terminator-source')) return;

        try {
            const resp = await fetch(`/api/layers/terminator?token=${encodeURIComponent(this.token)}`);
            if (resp.ok) {
                const geojson = await resp.json();
                const src = map.getSource('terminator-source');
                if (src) {
                    src.setData(geojson);
                }
            }
        } catch (e) {
            console.warn('[GlobeManager] Failed to update solar terminator:', e);
        }
    }

    /** Toggle atmospheric glow */
    setAtmosphere(visible) {
        this.atmosphereVisible = visible;
        const map = this.engine.map;
        if (!map) return;

        try {
            if (typeof map.setFog === 'function') {
                if (visible) {
                    map.setFog({
                        'range': [0.5, 10],
                        'color': '#020b18',
                        'horizon-blend': 0.15,
                        'high-color': '#00e5ff',
                        'space-color': '#000308',
                        'star-intensity': 0.6,
                    });
                } else {
                    map.setFog(null);
                }
            }
        } catch (e) {
            // Older projection or fallback
        }
    }

    /** Toggle terminator visibility */
    setTerminatorVisible(visible) {
        this.terminatorVisible = visible;
        const map = this.engine.map;
        if (!map || !map.getLayer('terminator-layer')) return;
        map.setLayoutProperty('terminator-layer', 'visibility', visible ? 'visible' : 'none');
    }

    /** Calculate great-circle arc points between two coordinates */
    static calculateGreatCircleArc(start, end, numPoints = 64) {
        const [lon1, lat1] = start;
        const [lon2, lat2] = end;

        const toRad = deg => (deg * Math.PI) / 180;
        const toDeg = rad => (rad * 180) / Math.PI;

        const rLat1 = toRad(lat1);
        const rLon1 = toRad(lon1);
        const rLat2 = toRad(lat2);
        const rLon2 = toRad(lon2);

        const d = 2 * Math.asin(
            Math.sqrt(
                Math.sin((rLat1 - rLat2) / 2) ** 2 +
                Math.cos(rLat1) * Math.cos(rLat2) * (Math.sin((rLon1 - rLon2) / 2) ** 2)
            )
        );

        if (d === 0) return [start, end];

        const arcCoords = [];
        for (let i = 0; i <= numPoints; i++) {
            const f = i / numPoints;
            const A = Math.sin((1 - f) * d) / Math.sin(d);
            const B = Math.sin(f * d) / Math.sin(d);

            const x = A * Math.cos(rLat1) * Math.cos(rLon1) + B * Math.cos(rLat2) * Math.cos(rLon2);
            const y = A * Math.cos(rLat1) * Math.sin(rLon1) + B * Math.cos(rLat2) * Math.sin(rLon2);
            const z = A * Math.sin(rLat1) + B * Math.sin(rLat2);

            const arcLat = toDeg(Math.atan2(z, Math.sqrt(x ** 2 + y ** 2)));
            const arcLon = toDeg(Math.atan2(y, x));
            arcCoords.push([arcLon, arcLat]);
        }

        return arcCoords;
    }
}
