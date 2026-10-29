#!/usr/bin/env node
// CR-Katalog-Harvest: kompletten Crunchyroll-Serien-Katalog aus den öffentlichen
// Sitemaps ziehen (GitHub-Runner-Route; funktioniert auch über andere Netze).
// Hintergrund (29.10.2026): Die A-Z-Seite /de/videos/alphabetical liefert nur eine
// client-seitige JS-Shell (kein __NEXT_DATA__, nichts parsbar – "Netflix-Manier").
// ABER: robots.txt verlinkt einen offenen Sitemap-Index mit Serien-Sitemaps pro
// Buchstabe (/sitemaps/series/A.xml?page=0 ... Z.xml, #.xml) – darin alle Serien-URLs
// mit CR-Serien-ID (G...) und Slug. Live getestet 29.10.2026: HTTP 200, kein Block,
// kein Geo-Redirect (im Gegensatz zu ADN).
// Caveats: (a) Sitemap = GLOBALER Katalog, NICHT DE-gefiltert – DE-Verfügbarkeit
// bleibt Titel-Ebene (streaming_rollcheck/JustWatch). (b) Keine lastmod-Daten –
// Aktualität nur über wöchentlichen Diff. (c) Der robots-erlaubte API-Endpunkt
// (cr-lab-akihabara-service) rate-limitet aggressiv (Cloudflare 1015 beim Test) und
// ist vermutlich nur Merch-Store – nicht nutzen. (d) RATE-LIMIT-ERFAHRUNG (29.10.):
// ~5 Abrufe in <1 h lösten einen temporären Cloudflare-Ban (1015/403) für die ganze
// Domain aus – wöchentlicher Lauf mit 28 Abrufen + 1500 ms Pause ist vermutlich ok,
// Runner-IPs rotieren; falls doch geblockt (Status blocked), PAUSE_MS erhöhen und
// erneut testen, erst bei dauerhaftem Block Route als tot dokumentieren.
// Ergebnis: cr_catalog.json (Serien-IDs + Slugs, sortiert) + cr_catalog_status.json
// (Diagnose je Buchstabe + Diff added/removed). Bei Block KEIN Crash: Status auf
// blocked, bestehender Katalog bleibt unangetastet (analog adn_catalog_fetch.mjs).

import { readFileSync, writeFileSync, existsSync } from "node:fs";

const SITEMAP_INDEX = "https://www.crunchyroll.com/sitemap.xml";
const CATALOG_FILE = "cr_catalog.json";
const STATUS_FILE = "cr_catalog_status.json";
const PAUSE_MS = 1500; // Rate-Limit-Respekt: CR bannt aggressiv (Cloudflare 1015, s. Header)
const FETCH_TIMEOUT_MS = 15000;
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function causeStr(e) {
  const cause = e?.cause;
  return cause?.code ?? cause?.message ?? String(cause ?? "unbekannt");
}

async function fetchText(url) {
  const res = await fetch(url, {
    method: "GET",
    redirect: "follow",
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    headers: { "User-Agent": UA, "Accept": "application/xml,text/xml,*/*" },
  });
  const body = await res.text();
  return { url, status: res.status, ok: res.ok, body };
}

function extractLocs(xml) {
  return [...xml.matchAll(/<loc>([^<]+)<\/loc>/g)].map(m => m[1].trim());
}

// Serien-Eintrag aus URL: /series/<ID>/<slug> -> { id, slug, url }
function parseSeriesUrl(u) {
  const m = u.match(/\/series\/([A-Z0-9]+)\/([a-z0-9-]+)/i);
  if (!m) return null;
  return { id: m[1], slug: m[2], url: u };
}

// --- Main ----------------------------------------------------------------------
const status = { updated: new Date().toISOString(), source: "crunchyroll.com sitemaps/series", sitemaps: {}, blocked: false, ok: false };
const byId = new Map();
let seriesSitemaps = [];
let stopReason = null;

// Schritt 1: Sitemap-Index holen und Serien-Sitemaps daraus ableiten
// (dynamisch statt hartkodiert A-Z: verträgt neue Buchstaben-/Seiten-Parameter automatisch)
let idx;
try {
  idx = await fetchText(SITEMAP_INDEX);
} catch (e) {
  writeFileSync(STATUS_FILE, JSON.stringify({ ...status, ok: false, stopReason: `Index-Fetch fehlgeschlagen: ${causeStr(e)}` }, null, 2));
  console.log(`Sitemap-Index nicht abrufbar (${causeStr(e)}) – nur Status geschrieben.`);
  process.exit(0);
}
if (!idx.ok) {
  writeFileSync(STATUS_FILE, JSON.stringify({ ...status, ok: false, blocked: true, stopReason: `Sitemap-Index HTTP ${idx.status}` }, null, 2));
  console.log(`Sitemap-Index blockt (HTTP ${idx.status}) – Route tot dokumentieren.`);
  process.exit(0);
}
seriesSitemaps = extractLocs(idx.body).filter(u => /\/sitemaps\/series\//.test(u));
status.seriesSitemapCount = seriesSitemaps.length;
console.log(`Sitemap-Index: HTTP ${idx.status}, ${seriesSitemaps.length} Serien-Sitemaps.`);

// Schritt 2: alle Serien-Sitemaps abholen und Serien extrahieren
let idxS = 0;
for (const sm of seriesSitemaps) {
  const label = decodeURIComponent(sm.split("/sitemaps/series/")[1] || String(idxS));
  idxS++;
  let r;
  try {
    r = await fetchText(sm);
  } catch (e) {
    status.sitemaps[label] = { ok: false, reason: "fetch-error", error: causeStr(e) };
    stopReason = `fetch-error bei ${label}`;
    break;
  }
  const challenge = !r.ok || /banned you temporarily|challenge|just a moment|error 1\d{3}/i.test(r.body.slice(0, 2000));
  if (challenge) {
    status.sitemaps[label] = { ok: false, http: r.status, blocked: true, reason: r.ok ? "anti-bot-block" : `http-${r.status}` };
    status.blocked = true;
    stopReason = `${label} blockt (HTTP ${r.status}${r.ok ? ", Anti-Bot" : ""})`;
    break;
  }
  const series = extractLocs(r.body).map(parseSeriesUrl).filter(Boolean);
  let newCount = 0;
  for (const s of series) {
    if (!byId.has(s.id)) { byId.set(s.id, s); newCount++; }
  }
  status.sitemaps[label] = { ok: true, http: r.status, seriesFound: series.length, newSeries: newCount };
  if (idxS < seriesSitemaps.length) await sleep(PAUSE_MS);
}

status.ok = !status.blocked && byId.size > 0;
status.count = byId.size;
status.stopReason = stopReason;
const hadPrevious = existsSync(CATALOG_FILE);

if (status.blocked && byId.size === 0) {
  writeFileSync(STATUS_FILE, JSON.stringify(status, null, 2));
  console.log("CR-Sitemaps blocken – Diagnose in cr_catalog_status.json, Katalogdatei unveraendert.");
} else if (byId.size === 0) {
  writeFileSync(STATUS_FILE, JSON.stringify(status, null, 2));
  console.log("Keine Serien extrahiert – nur Status geschrieben (Layout-Änderung? Parse pruefen).");
} else {
  // Diff gegen vorherigen Lauf
  let added = null, removed = null;
  try {
    if (hadPrevious) {
      const prev = JSON.parse(readFileSync(CATALOG_FILE, "utf8"));
      const prevIds = new Set((prev.series || []).map(s => s.id));
      const curIds = new Set(byId.keys());
      added = [...curIds].filter(id => !prevIds.has(id)).length;
      removed = [...prevIds].filter(id => !curIds.has(id)).length;
    }
  } catch { /* Diff optional */ }
  status.diff = hadPrevious ? { added, removed } : null;

  writeFileSync(STATUS_FILE, JSON.stringify(status, null, 2));
  writeFileSync(CATALOG_FILE, JSON.stringify({
    updated: status.updated,
    source: "https://www.crunchyroll.com/sitemaps/series/ (offizieller Sitemap-Index via /sitemap.xml)",
    note: "Globaler CR-Serien-Katalog aus Sitemaps – NICHT DE-gefiltert (DE-Verfügbarkeit = Titel-Ebene, streaming_rollcheck). " +
      "Serien-URLs ohne Locale; DE-Variante per /de/series/<id>/<slug> ableitbar, Verfgübarkeit aber geo-abhängig. " +
      "Neuzugänge/Abgänge über wöchentlichen Diff (status.diff).",
    count: byId.size,
    series: [...byId.values()].sort((a, b) => a.slug.localeCompare(b.slug)),
  }, null, 2));
  console.log(`Katalog geschrieben: ${byId.size} Serien (Sitemaps: ${seriesSitemaps.length}, diff=${JSON.stringify(status.diff)}).`);
}
