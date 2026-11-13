/**
 * Core Map Engine module for JARVIS Map Explorer.
 * Integrates MapLibre GL JS with unified 2D Mercator and 3D Globe projection support,
 * style switching, coordinate tracking, and marker management.
 */

export class MapEngine {
    constructor(containerId = 'map', options = {}) {
        this.containerId = containerId;
        this.currentProjection = options.projection || 'mercator';
        this.currentStyleKey = options.styleKey || 'dark';
        this.map = null;
        this.markers = [];
        this.onMouseMoveCallbacks = [];
        this.onClickCallbacks = [];

        // Base Map Styles Definition
        this.styles = {
            dark: {
                name: 'Dark HUD',
                url: 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',
            },
            streets: {
                name: 'Street Map',
                url: 'https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json',
            },
            light: {
                name: 'Light Carto',
                url: 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json',
            },
            satellite: {
                name: 'Satellite Imagery',
                definition: {
                    version: 8,
                    sources: {
                        'esri-satellite': {
                            type: 'raster',
                            tiles: [
                                'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
                            ],
                            tileSize: 256,
                            attribution: '© ESRI World Imagery',
                        },
                    },
                    layers: [
                        {
                            id: 'esri-satellite-layer',
                            type: 'raster',
                            source: 'esri-satellite',
                            minzoom: 0,
                            maxzoom: 19,
                        },
                    ],
                },
            },
            satellite_labels: {
                name: 'Satellite with Labels',
                definition: {
                    version: 8,
                    sources: {
                        'esri-satellite': {
                            type: 'raster',
                            tiles: [
                                'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
                            ],
                            tileSize: 256,
                            attribution: '© ESRI World Imagery',
                        },
                        'esri-boundaries': {
                            type: 'raster',
                            tiles: [
                                'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
                            ],
                            tileSize: 256,
                        },
                    },
                    layers: [
                        {
                            id: 'esri-satellite-base',
                            type: 'raster',
                            source: 'esri-satellite',
                            minzoom: 0,
                            maxzoom: 19,
                        },
                        {
                            id: 'esri-boundaries-overlay',
                            type: 'raster',
                            source: 'esri-boundaries',
                            minzoom: 0,
                            maxzoom: 19,
                        },
                    ],
                },
            },
            topo: {
                name: 'Topographic Contours',
                definition: {
                    version: 8,
                    sources: {
                        opentopomap: {
                            type: 'raster',
                            tiles: [
                                'https://tile.opentopomap.org/{z}/{x}/{y}.png',
                            ],
                            tileSize: 256,
                            attribution: '© OpenTopoMap contributors',
                        },
                    },
                    layers: [
                        {
                            id: 'opentopo-layer',
                            type: 'raster',
                            source: 'opentopomap',
                            minzoom: 0,
                            maxzoom: 17,
                        },
                    ],
                },
            },
        };
    }

    /** Initialize the MapLibre map instance */
    init(center = [75.1063, 12.3081], zoom = 12) {
        if (!window.maplibregl) {
            console.error('[MapEngine] MapLibre GL JS library not found!');
            return;
        }

        const styleObj = this._getStyleObject(this.currentStyleKey);

        this.map = new window.maplibregl.Map({
            container: this.containerId,
            style: styleObj,
            center: center,
            zoom: zoom,
            pitch: 0,
            bearing: 0,
            attributionControl: false,
        });

        // Set initial projection
        this.map.on('load', () => {
            this.setProjection(this.currentProjection);
            this._setupEvents();
            console.log('[MapEngine] Map initialized successfully.');
        });

        return this.map;
    }

    /** Switch map projection smoothly (2D Mercator <-> 3D Globe) */
    setProjection(projectionType) {
        if (!this.map) return;
        const target = projectionType === 'globe' || projectionType === '3d' ? 'globe' : 'mercator';
        this.currentProjection = target;

        try {
            if (typeof this.map.setProjection === 'function') {
                this.map.setProjection({ type: target });
            }
        } catch (e) {
            console.warn('[MapEngine] Projection change error:', e);
        }
    }

    /** Switch base map visual style while preserving layers and camera */
    setStyle(styleKey, customUrl = '') {
        if (!this.map) return;
        this.currentStyleKey = styleKey;
        let targetStyle;

        if (styleKey === 'custom' && customUrl) {
            targetStyle = customUrl;
        } else {
            targetStyle = this._getStyleObject(styleKey);
        }

        const currentCenter = this.map.getCenter();
        const currentZoom = this.map.getZoom();
        const currentPitch = this.map.getPitch();
        const currentBearing = this.map.getBearing();

        this.map.setStyle(targetStyle);

        this.map.once('style.load', () => {
            this.map.setCenter(currentCenter);
            this.map.setZoom(currentZoom);
            this.map.setPitch(currentPitch);
            this.map.setBearing(currentBearing);
            this.setProjection(this.currentProjection);
            // Re-render markers and overlays
            this.refreshMarkers();
        });
    }

    /** Fly camera to coordinates with smooth animation */
    flyTo(lon, lat, zoom = 13, pitch = 0, bearing = 0) {
        if (!this.map) return;
        this.map.flyTo({
            center: [lon, lat],
            zoom: zoom,
            pitch: pitch,
            bearing: bearing,
            essential: true,
            duration: 1800,
        });
    }

    /** Add a customizable marker */
    addMarker(lon, lat, options = {}) {
        if (!this.map || !window.maplibregl) return null;

        let el = options.element;
        if (!el) {
            el = document.createElement('div');
            el.className = 'custom-marker';
            el.style.width = '24px';
            el.style.height = '24px';
            el.style.borderRadius = '50%';
            el.style.background = options.color || '#00e5ff';
            el.style.boxShadow = `0 0 10px ${options.color || '#00e5ff'}`;
            el.style.border = '2px solid #ffffff';
            el.style.cursor = 'pointer';
        }

        const marker = new window.maplibregl.Marker({ element: el })
            .setLngLat([lon, lat])
            .addTo(this.map);

        if (options.popupHtml) {
            const popup = new window.maplibregl.Popup({ offset: 15 })
                .setHTML(options.popupHtml);
            marker.setPopup(popup);
        }

        this.markers.push(marker);
        return marker;
    }

    /** Clear all active user markers */
    clearMarkers() {
        this.markers.forEach(m => m.remove());
        this.markers = [];
    }

    refreshMarkers() {
        this.markers.forEach(m => m.addTo(this.map));
    }

    onMouseMove(callback) {
        this.onMouseMoveCallbacks.push(callback);
    }

    onClick(callback) {
        this.onClickCallbacks.push(callback);
    }

    _setupEvents() {
        this.map.on('mousemove', (e) => {
            const { lng, lat } = e.lngLat;
            const zoom = this.map.getZoom();
            const pitch = this.map.getPitch();
            const bearing = this.map.getBearing();

            this.onMouseMoveCallbacks.forEach(cb => cb({
                lng: lng,
                lat: lat,
                zoom: zoom,
                pitch: pitch,
                bearing: bearing,
            }));
        });

        this.map.on('click', (e) => {
            const { lng, lat } = e.lngLat;
            this.onClickCallbacks.forEach(cb => cb({ lng, lat, point: e.point }));
        });
    }

    _getStyleObject(key) {
        const entry = this.styles[key] || this.styles.dark;
        return entry.url || entry.definition;
    }
}
