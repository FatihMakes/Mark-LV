/**
 * Search & Geocoding module for JARVIS Map Explorer.
 * Coordinates global place queries, autocomplete suggestions, and coordinate jumping.
 */

export class SearchManager {
    constructor(mapEngine, sessionToken = '') {
        this.engine = mapEngine;
        this.token = sessionToken;
        this.recentSearches = JSON.parse(localStorage.getItem('jarvis_recent_searches') || '[]');
        this.activeSearchMarker = null;
        this.debounceTimer = null;
    }

    init() {
        this._bindUI();
        this.renderRecentSearches();
    }

    _bindUI() {
        const input = document.getElementById('global-search');
        const dropdown = document.getElementById('search-results-dropdown');
        const clearBtn = document.getElementById('search-clear-btn');

        if (input) {
            input.addEventListener('input', () => {
                const q = input.value.trim();
                if (clearBtn) clearBtn.classList.toggle('hidden', q.length === 0);

                clearTimeout(this.debounceTimer);
                if (q.length < 2) {
                    if (dropdown) dropdown.classList.add('hidden');
                    return;
                }
                this.debounceTimer = setTimeout(() => this.fetchSuggestions(q), 280);
            });

            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    const q = input.value.trim();
                    if (q) this.executeSearch(q);
                    if (dropdown) dropdown.classList.add('hidden');
                }
            });
        }

        if (clearBtn) {
            clearBtn.addEventListener('click', () => {
                if (input) {
                    input.value = '';
                    clearBtn.classList.add('hidden');
                    if (dropdown) dropdown.classList.add('hidden');
                }
            });
        }

        // Close dropdown when clicking outside
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.search-container')) {
                dropdown?.classList.add('hidden');
            }
        });

        // Quick region chips
        document.querySelectorAll('#tab-search .chip').forEach(chip => {
            chip.addEventListener('click', () => {
                const lat = parseFloat(chip.dataset.lat);
                const lon = parseFloat(chip.dataset.lon);
                const zoom = parseFloat(chip.dataset.zoom) || 12;
                this.engine.flyTo(lon, lat, zoom);
            });
        });

        // Coordinate parser input
        document.getElementById('coord-jump-btn')?.addEventListener('click', () => {
            const raw = document.getElementById('coord-input')?.value.trim();
            if (raw) this.executeSearch(raw);
        });
    }

    async fetchSuggestions(query) {
        const dropdown = document.getElementById('search-results-dropdown');
        if (!dropdown) return;

        try {
            const resp = await fetch(`/api/geocode?q=${encodeURIComponent(query)}&limit=6&token=${encodeURIComponent(this.token)}`);
            const data = await resp.json();
            const results = data.results || [];

            dropdown.innerHTML = '';
            if (results.length === 0) {
                dropdown.classList.add('hidden');
                return;
            }

            results.forEach(r => {
                const item = document.createElement('div');
                item.className = 'dropdown-item';
                item.innerHTML = `
                    <span class="d-title">${r.name}</span>
                    <span class="d-sub">${r.display_name || r.country || ''}</span>
                `;

                item.addEventListener('click', () => {
                    this.selectResult(r);
                    dropdown.classList.add('hidden');
                    const input = document.getElementById('global-search');
                    if (input) input.value = r.name;
                });

                dropdown.appendChild(item);
            });

            dropdown.classList.remove('hidden');
        } catch (e) {
            console.warn('[SearchManager] Suggestion fetch error:', e);
        }
    }

    async executeSearch(query) {
        try {
            const resp = await fetch(`/api/geocode?q=${encodeURIComponent(query)}&limit=1&token=${encodeURIComponent(this.token)}`);
            const data = await resp.json();
            if (data.results && data.results.length > 0) {
                this.selectResult(data.results[0]);
                this.addRecentSearch(data.results[0].name);
            } else {
                alert(`No location found for: ${query}`);
            }
        } catch (e) {
            alert('Search error: ' + e.message);
        }
    }

    selectResult(r) {
        if (this.activeSearchMarker) {
            this.activeSearchMarker.remove();
        }

        this.engine.flyTo(r.longitude, r.latitude, 13);

        const el = document.createElement('div');
        el.className = 'custom-marker';
        el.style.width = '24px';
        el.style.height = '24px';
        el.style.borderRadius = '50%';
        el.style.background = '#00e5ff';
        el.style.border = '3px solid #ffffff';
        el.style.boxShadow = '0 0 16px #00e5ff';

        this.activeSearchMarker = new window.maplibregl.Marker({ element: el })
            .setLngLat([r.longitude, r.latitude])
            .setPopup(new window.maplibregl.Popup().setHTML(`<b>${r.name}</b><br><small>${r.display_name || ''}</small>`))
            .addTo(this.engine.map);

        this.activeSearchMarker.togglePopup();
        this.addRecentSearch(r.name);
    }

    addRecentSearch(name) {
        if (!name) return;
        this.recentSearches = this.recentSearches.filter(s => s !== name);
        this.recentSearches.unshift(name);
        if (this.recentSearches.length > 10) this.recentSearches.pop();
        localStorage.setItem('jarvis_recent_searches', JSON.stringify(this.recentSearches));
        this.renderRecentSearches();
    }

    renderRecentSearches() {
        const list = document.getElementById('recent-searches-list');
        if (!list) return;
        list.innerHTML = '';

        if (this.recentSearches.length === 0) {
            list.innerHTML = '<li class="empty-hint">No recent searches yet.</li>';
            return;
        }

        this.recentSearches.forEach(term => {
            const li = document.createElement('li');
            li.style.cursor = 'pointer';
            li.style.padding = '6px 0';
            li.style.color = '#e0f7fa';
            li.textContent = `🔍 ${term}`;
            li.addEventListener('click', () => {
                this.executeSearch(term);
                const input = document.getElementById('global-search');
                if (input) input.value = term;
            });
            list.appendChild(li);
        });
    }
}
