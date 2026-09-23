/* Canonical-host redirect (ui.canonical_url). Runs first in <head>, before
 * anything renders, and steers the browser to the canonical HTTPS host so TLS
 * and passkeys are the default. Client-side on purpose: the page loads first,
 * so a dead hostname never strands the operator on the local IP. Escape
 * hatches: `?local` in the URL (also sets a sticky flag) or the sticky
 * `mbg-stay-local` flag in localStorage. The target comes from the
 * <meta name="mbg-canonical"> the server renders — an HTML attribute, never
 * code — so this file is static and the CSP can forbid inline script. */
(function () {
    var meta = document.querySelector('meta[name="mbg-canonical"]');
    var C = meta && meta.content;
    if (!C) return;
    try {
        var h = new URL(C).host;
        var p = new URLSearchParams(location.search);
        if (p.has('local')) {
            try { localStorage.setItem('mbg-stay-local', '1'); } catch (e) {}
            return;
        }
        if (localStorage.getItem('mbg-stay-local') === '1') return;
        if (location.host === h) return;
        location.replace(C.replace(/\/$/, '') + location.pathname + location.search + location.hash);
    } catch (e) {}
})();
