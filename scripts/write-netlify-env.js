#!/usr/bin/env node
/**
 * Writes backend/static/env.js from process.env.WT_API_URL (Netlify env var).
 * Run automatically on Netlify build via netlify.toml.
 */
const fs = require("fs");
const path = require("path");

const url = process.env.WT_API_URL || "";
const out = path.join(__dirname, "..", "backend", "static", "env.js");
fs.writeFileSync(out, `window.__ENV_WT_API__=${JSON.stringify(url)};\n`);
console.log("[write-netlify-env]", out, url ? `WT_API_URL set (${url.length} chars)` : "WT_API_URL empty");
