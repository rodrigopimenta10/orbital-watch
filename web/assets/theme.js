/* Restore the stored theme before first paint.
 *
 * Loaded as a blocking script in <head> on purpose. Deferring this to
 * DOMContentLoaded makes the page paint in the OS theme and then snap to the
 * chosen one, which reads as a bug.
 *
 * It lives in its own file rather than inline so the Content-Security-Policy
 * can be a plain `script-src 'self'`. An inline script would need its sha256
 * in the CSP, and that hash breaks silently the moment anyone edits a
 * character of it.
 */
(function () {
  "use strict";
  try {
    var stored = localStorage.getItem("orbital-watch-theme");
    if (stored === "light" || stored === "dark") {
      document.documentElement.setAttribute("data-theme", stored);
    }
  } catch (e) {
    /* private mode: fall through to the OS preference */
  }
})();
