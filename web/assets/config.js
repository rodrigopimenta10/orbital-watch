/* Where the page finds the document.
 *
 * This committed copy describes a local build: `python -m orbital_watch.job`
 * puts the document at live/latest.json beside the site, standing in for the
 * public bucket. The deploy replaces this file (orbital_watch.site) with one
 * that names the real bucket; nothing else in web/ differs between a laptop
 * and production.
 */
window.ORBITAL_WATCH_CONFIG = {
  liveDocumentUrl: "live/latest.json",
  bakedDocumentUrl: "data/latest.json",
};
