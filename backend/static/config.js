/**
 * API base URL for fetch(). Leave empty when the HTML is served by the same FastAPI
 * process (e.g. one Render URL for UI + API — no extra domain or DNS).
 *
 * Priority:
 * 1) window.__ENV_WT_API__ from env.js (written on Netlify build from WT_API_URL).
 * 2) WT_DEPLOYED_API below (optional, for non-Netlify static hosts).
 * 3) ?api=https://… once (saved in localStorage).
 * 4) localStorage wt_api_base
 */

const WT_DEPLOYED_API = "";

(function () {
  if (typeof window === "undefined") return;

  function normalizeBase(raw) {
    if (raw == null) return "";
    let s = String(raw).trim();
    if (!s) return "";
    s = s.replace(/\/+$/, "");
    if (!/^https?:\/\//i.test(s)) s = "https://" + s;
    return s;
  }

  function remember(base) {
    try {
      localStorage.setItem("wt_api_base", base);
    } catch (e) {
      /* private mode */
    }
  }

  const fromNetlifyBuild = normalizeBase(
    typeof window.__ENV_WT_API__ !== "undefined" ? window.__ENV_WT_API__ : ""
  );
  if (fromNetlifyBuild) {
    window.__WT_API_BASE__ = fromNetlifyBuild;
    remember(fromNetlifyBuild);
    return;
  }

  const fromConst = normalizeBase(WT_DEPLOYED_API);
  if (fromConst) {
    window.__WT_API_BASE__ = fromConst;
    remember(fromConst);
    return;
  }

  const params = new URLSearchParams(window.location.search);
  const fromQuery = params.get("api");
  if (fromQuery) {
    const base = normalizeBase(decodeURIComponent(fromQuery));
    if (base) {
      window.__WT_API_BASE__ = base;
      remember(base);
      params.delete("api");
      const qs = params.toString();
      const newUrl = window.location.pathname + (qs ? "?" + qs : "") + window.location.hash;
      window.history.replaceState({}, "", newUrl);
      return;
    }
  }

  try {
    const stored = normalizeBase(localStorage.getItem("wt_api_base"));
    if (stored) {
      window.__WT_API_BASE__ = stored;
      return;
    }
  } catch (e) {}

  window.__WT_API_BASE__ = "";
})();
