/**
 * Settings & Configuration module for JARVIS Map Explorer.
 */

export class SettingsManager {
    constructor(mapEngine, sessionToken = '') {
        this.engine = mapEngine;
        this.token = sessionToken;
        this.lowPerfMode = false;
        this.units = 'metric';
    }

    init() {
        this._bindUI();
        this.loadDiagnostics();
    }

    _bindUI() {
        // Low performance toggle
        const lowPerfToggle = document.getElementById('cfg-low-perf');
        if (lowPerfToggle) {
            lowPerfToggle.addEventListener('change', () => {
                this.lowPerfMode = lowPerfToggle.checked;
                this.applyPerformanceMode();
            });
        }

        // Units selection
        const unitsSelect = document.getElementById('cfg-units');
        if (unitsSelect) {
            unitsSelect.addEventListener('change', () => {
                this.units = unitsSelect.value;
            });
        }
    }

    applyPerformanceMode() {
        const map = this.engine.map;
        if (!map) return;

        if (this.lowPerfMode) {
            // Disable heavy continuous fog and reduce particle overhead
            try {
                if (typeof map.setFog === 'function') map.setFog(null);
            } catch (e) {}
            console.log('[SettingsManager] Low GPU performance mode engaged.');
        } else {
            console.log('[SettingsManager] High fidelity rendering enabled.');
        }
    }

    async loadDiagnostics() {
        try {
            const resp = await fetch(`/api/status?token=${encodeURIComponent(this.token)}`);
            const data = await resp.json();
            const diagStatus = document.getElementById('diag-server-status');
            if (diagStatus) {
                diagStatus.textContent = `127.0.0.1:${data.port} (Loopback Online)`;
            }
        } catch (e) {
            const diagStatus = document.getElementById('diag-server-status');
            if (diagStatus) diagStatus.textContent = 'Offline / Connecting';
        }
    }
}
