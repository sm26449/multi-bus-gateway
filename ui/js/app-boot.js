/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// boot: instantiate the app


// Initialize app
document.addEventListener('DOMContentLoaded', () => {
    window.app = new JanitzaMonitor();
});
