/**
 * open-agc-train plugin — Frontend module
 * Dynamically loaded by main Open-AGC app when plugin is installed.
 */
(function() {
    'use strict';

    window.OpenAGCTrain = {
        loaded: false,

        init: function(wsEndpoint) {
            if (this.loaded) return;
            this.loaded = true;
            console.log('[Train Plugin] Initializing...');
            this._registerWebSocketHandlers();
            this._showTrainingMenu();
        },

        _registerWebSocketHandlers: function() {
            // WebSocket handlers for training progress are already registered
            // in the main app. This is a placeholder for plugin-specific handlers.
            console.log('[Train Plugin] WebSocket handlers registered');
        },

        _showTrainingMenu: function() {
            // Show training sidebar sections
            document.querySelectorAll('.nav-training-item, [data-section="training"]')
                .forEach(el => el.style.display = '');
            console.log('[Train Plugin] Training menu shown');
        },

        // ── Public API exposed for main app ──
        isAvailable: function() {
            return this.loaded;
        }
    };
})();
