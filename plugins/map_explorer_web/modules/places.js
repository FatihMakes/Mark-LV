/**
 * Places Explorer, Curated Destinations & Bookmarks module for JARVIS Map Explorer.
 */

export class PlacesManager {
    constructor(mapEngine, sessionToken = '') {
        this.engine = mapEngine;
        this.token = sessionToken;
        this.activeCategory = '';
        this.placeMarkers = [];
        this.savedPlaces = JSON.parse(localStorage.getItem('jarvis_saved_places') || '[]');
    }

    init() {
        this._bindUI();
        this.renderSavedPlaces();
        this.loadDestinations();
    }

    _bindUI() {
        // Category cards click
        document.querySelectorAll('.cat-card').forEach(card => {
            card.addEventListener('click', () => {
                const cat = card.dataset.cat || '';
                this.activeCategory = cat;
                this.searchNearby(cat);
            });
        });

        // Radius slider
        const slider = document.getElementById('places-radius-slider');
        const radiusVal = document.getElementById('places-radius-val');
        if (slider && radiusVal) {
            slider.addEventListener('input', () => {
                radiusVal.textContent = slider.value + ' km';
            });
            slider.addEventListener('change', () => {
                if (this.activeCategory) this.searchNearby(this.activeCategory);
            });
        }

        // Destination category chips
        document.querySelectorAll('#dest-category-chips .chip').forEach(chip => {
            chip.addEventListener('click', () => {
                document.querySelectorAll('#dest-category-chips .chip').forEach(c => c.classList.remove('active'));
                chip.classList.add('active');
                const cat = chip.dataset.destCat || '';
                this.loadDestinations(cat);
            });
        });
    }

    async searchNearby(category) {
        const map = this.engine.map;
        if (!map) return;
        const center = map.getCenter();
        const radius = document.getElementById('places-radius-slider')?.value || 25;

        const countLabel = document.getElementById('places-count-label');
        if (countLabel) countLabel.textContent = `SEARCHING ${category.toUpperCase()}...`;

        try {
            const resp = await fetch(`/api/places?category=${encodeURIComponent(category)}&lat=${center.lat}&lon=${center.lng}&radius=${radius}&token=${encodeURIComponent(this.token)}`);
            const data = await resp.json();
            const places = data.places || [];

            this.clearPlaceMarkers();
            this.renderPlacesList(places, category);

            if (countLabel) {
                countLabel.textContent = `${places.length} ${category.toUpperCase()} FOUND WITHIN ${radius} KM`;
            }

            // Add markers
            places.forEach(p => {
                const el = document.createElement('div');
                el.className = 'custom-marker';
                el.style.width = '20px';
                el.style.height = '20px';
                el.style.borderRadius = '50%';
                el.style.background = '#00e5ff';
                el.style.border = '2px solid #ffffff';
                el.style.boxShadow = '0 0 8px #00e5ff';
                el.style.cursor = 'pointer';

                const m = new window.maplibregl.Marker({ element: el })
                    .setLngLat([p.longitude, p.latitude])
                    .setPopup(new window.maplibregl.Popup().setHTML(`<b>${p.name}</b><br><small>${p.distance_km} km away</small>`))
                    .addTo(map);

                el.addEventListener('click', () => {
                    this.showPlaceDetails(p);
                });

                this.placeMarkers.push(m);
            });
        } catch (e) {
            console.error('[PlacesManager] Error fetching places:', e);
        }
    }

    renderPlacesList(places, category) {
        const container = document.getElementById('places-list');
        if (!container) return;
        container.innerHTML = '';

        if (places.length === 0) {
            container.innerHTML = '<div class="empty-hint">No locations found within selected radius. Try expanding the radius or panning map.</div>';
            return;
        }

        places.forEach(p => {
            const card = document.createElement('div');
            card.className = 'place-item-card';
            card.innerHTML = `
                <div class="card-head">
                    <span class="card-title">${p.name}</span>
                    <span class="card-badge">${p.distance_km} km</span>
                </div>
                <div class="card-meta">${p.address || p.category || 'Point of Interest'}</div>
                <div class="card-actions">
                    <button class="btn-tiny fly-btn">Fly To</button>
                    <button class="btn-tiny nav-btn">Directions</button>
                    <button class="btn-tiny save-btn">★ Save</button>
                </div>
            `;

            card.querySelector('.fly-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                this.engine.flyTo(p.longitude, p.latitude, 14);
                this.showPlaceDetails(p);
            });

            card.querySelector('.nav-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                const destInput = document.getElementById('route-destination');
                if (destInput) destInput.value = p.name;
                document.querySelector('.nav-tab[data-tab="routes"]')?.click();
            });

            card.querySelector('.save-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                this.savePlace(p);
            });

            card.addEventListener('click', () => {
                this.engine.flyTo(p.longitude, p.latitude, 14);
                this.showPlaceDetails(p);
            });

            container.appendChild(card);
        });
    }

    async loadDestinations(category = '') {
        const container = document.getElementById('destinations-list');
        if (!container) return;

        try {
            const resp = await fetch(`/api/destinations?category=${encodeURIComponent(category)}&token=${encodeURIComponent(this.token)}`);
            const data = await resp.json();
            const dests = data.destinations || [];

            container.innerHTML = '';
            dests.forEach(d => {
                const card = document.createElement('div');
                card.className = 'dest-item-card';
                card.innerHTML = `
                    <div class="card-head">
                        <span class="card-title">${d.name}</span>
                        <span class="card-badge">${d.category}</span>
                    </div>
                    <div class="card-meta" style="color:#00e5ff;margin-bottom:3px;">${d.region}</div>
                    <div class="card-meta">${d.description}</div>
                    <div class="card-actions">
                        <button class="btn-tiny fly-btn">Fly To Destination</button>
                        <button class="btn-tiny nav-btn">Directions</button>
                    </div>
                `;

                card.querySelector('.fly-btn').addEventListener('click', (e) => {
                    e.stopPropagation();
                    this.engine.flyTo(d.longitude, d.latitude, 13);
                    this.showPlaceDetails(d);
                });

                card.querySelector('.nav-btn').addEventListener('click', (e) => {
                    e.stopPropagation();
                    const destInput = document.getElementById('route-destination');
                    if (destInput) destInput.value = d.name;
                    document.querySelector('.nav-tab[data-tab="routes"]')?.click();
                });

                card.addEventListener('click', () => {
                    this.engine.flyTo(d.longitude, d.latitude, 13);
                    this.showPlaceDetails(d);
                });

                container.appendChild(card);
            });
        } catch (e) {
            console.error('[PlacesManager] Failed to load destinations:', e);
        }
    }

    showPlaceDetails(place) {
        const panel = document.getElementById('info-panel');
        const title = document.getElementById('info-panel-title');
        const body = document.getElementById('info-panel-body');
        if (!panel || !body) return;

        panel.classList.remove('info-panel-collapsed');
        if (title) title.textContent = 'PLACE INTELLIGENCE';

        const latDMS = Math.abs(place.latitude).toFixed(4) + '° ' + (place.latitude >= 0 ? 'N' : 'S');
        const lonDMS = Math.abs(place.longitude).toFixed(4) + '° ' + (place.longitude >= 0 ? 'E' : 'W');

        body.innerHTML = `
            <div class="section-block">
                <h3 style="color:#ffffff;font-size:15px;margin-bottom:4px;">${place.name}</h3>
                <span class="card-badge">${place.category || 'Point of Interest'}</span>
                <p style="color:#78909c;font-size:11.5px;margin-top:8px;line-height:1.4;">${place.description || place.address || place.region || 'Verified Geographic Entity'}</p>
            </div>

            <div class="telemetry-box" style="margin-bottom:14px;">
                <div class="telemetry-row">
                    <span class="t-key">Coordinates:</span>
                    <span class="t-val">${place.latitude}, ${place.longitude}</span>
                </div>
                <div class="telemetry-row">
                    <span class="t-key">DMS:</span>
                    <span class="t-val">${latDMS}, ${lonDMS}</span>
                </div>
                ${place.distance_km ? `
                <div class="telemetry-row">
                    <span class="t-key">Distance:</span>
                    <span class="t-val">${place.distance_km} km</span>
                </div>` : ''}
                <div class="telemetry-row">
                    <span class="t-key">Source:</span>
                    <span class="t-val">${place.source || 'JARVIS Earth Database'}</span>
                </div>
            </div>

            <div class="btn-group-row">
                <button id="detail-nav-btn" class="btn-primary">NAVIGATE HERE</button>
                <button id="detail-save-btn" class="btn-secondary">★ SAVE PLACE</button>
            </div>
        `;

        document.getElementById('detail-nav-btn')?.addEventListener('click', () => {
            const destInput = document.getElementById('route-destination');
            if (destInput) destInput.value = place.name;
            document.querySelector('.nav-tab[data-tab="routes"]')?.click();
        });

        document.getElementById('detail-save-btn')?.addEventListener('click', () => {
            this.savePlace(place);
        });
    }

    savePlace(place) {
        if (this.savedPlaces.some(p => p.name === place.name)) {
            alert(`${place.name} is already in your saved bookmarks.`);
            return;
        }
        this.savedPlaces.push({
            name: place.name,
            category: place.category || 'Saved Place',
            latitude: place.latitude,
            longitude: place.longitude,
            savedAt: new Date().toISOString()
        });
        localStorage.setItem('jarvis_saved_places', JSON.stringify(this.savedPlaces));
        this.renderSavedPlaces();
        alert(`Saved ${place.name} to bookmarks!`);
    }

    renderSavedPlaces() {
        const container = document.getElementById('saved-places-list');
        if (!container) return;
        container.innerHTML = '';

        if (this.savedPlaces.length === 0) {
            container.innerHTML = '<div class="empty-hint">No saved bookmarks yet. Click ★ Save Place on any location card.</div>';
            return;
        }

        this.savedPlaces.forEach((p, idx) => {
            const card = document.createElement('div');
            card.className = 'place-item-card';
            card.innerHTML = `
                <div class="card-head">
                    <span class="card-title">${p.name}</span>
                    <span class="card-badge">Saved</span>
                </div>
                <div class="card-meta">Coords: ${p.latitude}, ${p.longitude}</div>
                <div class="card-actions">
                    <button class="btn-tiny fly-btn">Fly To</button>
                    <button class="btn-tiny del-btn">Remove</button>
                </div>
            `;

            card.querySelector('.fly-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                this.engine.flyTo(p.longitude, p.latitude, 13);
            });

            card.querySelector('.del-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                this.savedPlaces.splice(idx, 1);
                localStorage.setItem('jarvis_saved_places', JSON.stringify(this.savedPlaces));
                this.renderSavedPlaces();
            });

            card.addEventListener('click', () => {
                this.engine.flyTo(p.longitude, p.latitude, 13);
            });

            container.appendChild(card);
        });
    }

    clearPlaceMarkers() {
        this.placeMarkers.forEach(m => m.remove());
        this.placeMarkers = [];
    }
}
