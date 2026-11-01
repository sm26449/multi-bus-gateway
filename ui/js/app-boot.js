/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// boot: instantiate the app


// Initialize app
document.addEventListener('DOMContentLoaded', () => {
    window.app = new JanitzaMonitor();
    // a device write refused because the device changed elsewhere (ADR-0030)
    if (window.DeviceStore) window.DeviceStore.onConflict = c => window.app._ddvOnConflict(c);
});
