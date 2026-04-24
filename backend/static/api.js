/**
 * API base for fetch() and <img src>. Set window.__WT_API_BASE__ in config.js
 * when the HTML is on Netlify and the FastAPI server is elsewhere.
 */
function apiUrl(path) {
  const base = String(typeof window !== "undefined" && window.__WT_API_BASE__ ? window.__WT_API_BASE__ : "").replace(
    /\/$/,
    ""
  );
  const p = path.startsWith("/") ? path : `/${path}`;
  return base + p;
}
