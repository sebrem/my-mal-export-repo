#!/usr/bin/env node
// ADN-Katalog-Harvest: kompletten Animes-Katalog von animationdigitalnetwork.com/de
// als adn_catalog.json ziehen (GitHub-Runner-Route).
// Hintergrund (29.10.2026): Die Katalogseite /de/video ist eine Next.js-App und
// liefert pro Seite (48 Titel, Pagination ?page=1..N) im __NEXT_DATA__ den Query
// "catalogShows" mit state.data.shows[] + state.data.total (Stand 29.10.2026: 268).
// Der Mistral-Proxy wird von ADN geblockt (403, Anti-Bot-Heuristik – s. Projekt-Doku
// "RSS-Feeds Crunchyroll & ADN"), GitHub-Runner kommen durch (analog ADN-News-Feed
// und streaming_rollcheck V8.5 ADN-Show-Seiten-Check).
// Caveats: (a) available wird serverseitig aus Abruf-Standort berechnet (Geo).
// (b) /_next/data und Sitemap sind CloudFront-blockt – HTML-Katalogseiten sind offen.
// (c) Pagination-Seitenzahl steht nicht im JSON; es wird geblättert, bis eine Seite
// keine neuen Titel liefert oder total erreicht ist (Safety-Maximum ADN_MAX_PAGES).
// Ergebnis: adn_catalog.json (Katalog) + adn_catalog_status.json (Diagnose).
// Bei Block (403/Challenge) KEIN Crash: Status auf blocked, bestehender Katalog
// bleibt unangetastet (analog anime2you_feed_fetch.mjs).

import { readFileSync, writeFileSync, existsSync } from "node:fs";

const BASE_URL = "https://animationdigitalnetwork.com/de/video";
const CATALOG_FILE = "adn_catalog.json";
const STATUS_FILE = "adn_catalog_status.json";
const MAX_PAGES = Number(process.env.ADN_MAX_PAGES || "10"); // Safety-Stop (268/48 = 6 Seiten)
const PAUSE_MS = 1500; // Rate-Limit-Respekt ggü. ADN-HTML-Seiten (wie Rollcheck V8.5)
const FETCH_TIMEOUT_MS = 15000;
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function causeStr(e) {
  const cause = e?.cause;
  return cause?.code ?? cause?.message ?? String(cause ?? "unbekannt");
}

// Katalogfelder, die für Streaming-Monitoring/MAL-Abgleich relevant sind
// (Bilder/SEO/Microdata bewusst weggelassen – Dateigröße klein halten)
function slimShow(s) {
  return {
    id: s.id,
    title: s.title,
    originalTitle: s.originalTitle ?? null,
    url: s.url ?? null,
    urlPath: s.urlPath ?? null,
    type: s.type ?? null,
    episodeCount: s.episodeCount ?? null,
    age: s.age ?? null,
    languages: s.languages ?? [],
    genres: s.genres ?? [],
    free: s.free ?? null,
    simulcast: s.simulcast ?? null,
    available: s.available ?? null, // Geo-Caveat: Sicht des Abruf-Standorts
    newEpisode: s.newEpisode ?? null,
    nextVideoReleaseDate: s.nextVideoReleaseDate ?? null,
    qualities: s.qualities ?? [],
    basedOn: s.basedOn ?? null,
    distributions: s.distributions ?? null,
    rating: s.rating ?? null,
    ratingsCount: s.ratingsCount ?? null,
  };
}

async function fetchPage(page) {
  const url = `${BASE_URL}?page=${page}`;
  const res = await fetch(url, {
    method: "GET",
    redirect: "follow",
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    headers: {
      "User-Agent": UA,
      "Accept": "text/html,application/xhtml+xml",
      "Accept-Language": "de-DE,de;q=0.9,fr;q=0.5",
    },
  });
  const body = await res.text();
  return { page, url, status: res.status, ok: res.ok, body };
}

function parseCatalog(html) {
  const m = html.match(/<script id="__NEXT_DATA__" type="application\/json">([\s\S]*?)<\/script>/);
  if (!m) return null;
  let queries;
  try {
    queries = (JSON.parse(m[1])?.props?.pageProps?.dehydratedState?.queries) || [];
  } catch { return null; }
  for (const q of queries) {
    const key = Array.isArray(q?.queryKey) ? q.queryKey[0] : null;
    const data = q?.state?.data;
    if (key === "catalogShows" && data && Array.isArray(data.shows)) {
      return { shows: data.shows, total: typeof data.total === "number" ? data.total : null };
    }
  }
  return null;
}

// --- Main ----------------------------------------------------------------------
const status = { updated: new Date().toISOString(), source: BASE_URL, pages: {}, blocked: false, ok: false };
const byId = new Map();
let totalReported = null;
let stopReason = null;

for (let page = 1; page <= MAX_PAGES; page++) {
  let r;
  try {
    r = await fetchPage(page);
  } catch (e) {
    status.pages[page] = { ok: false, http: 0, reason: "fetch-error", error: causeStr(e) };
    stopReason = `fetch-error auf Seite ${page}`;
    break;
  }
  const challenge = !r.ok && /403 ERROR|Request blocked|challenge|just a moment|enable javascript/i.test(r.body);
  if (!r.ok || challenge) {
    status.pages[page] = {
      ok: false, http: r.status, blocked: true,
      reason: challenge ? "anti-bot-block" : `http-${r.status}`,
      bodySnippet: r.body.replace(/\s+/g, " ").slice(0, 200),
    };
    status.blocked = true;
    stopReason = `Seite ${page} blockt (HTTP ${r.status}${challenge ? ", Anti-Bot" : ""})`;
    break;
  }
  const cat = parseCatalog(r.body);
  if (!cat || cat.shows.length === 0) {
    status.pages[page] = { ok: true, http: r.status, showsFound: 0 };
    stopReason = `Seite ${page} ohne catalogShows-Daten (Ende der Pagination oder Layout-Änderung)`;
    break;
  }
  if (cat.total !== null) totalReported = cat.total;
  let newCount = 0;
  for (const s of cat.shows) {
    if (s?.id != null && !byId.has(s.id)) { byId.set(s.id, slimShow(s)); newCount++; }
  }
  status.pages[page] = { ok: true, http: r.status, showsFound: cat.shows.length, newShows: newCount };
  console.log(`Seite ${page}: HTTP ${r.status}, ${cat.shows.length} Titel (${newCount} neu), total=${cat.total}.`);
  if (newCount === 0) { stopReason = `Seite ${page} brachte keine neuen Titel – Pagination-Ende.`; break; }
  if (totalReported !== null && byId.size >= totalReported) { stopReason = `total (${totalReported}) erreicht.`; break; }
  if (page < MAX_PAGES) await sleep(PAUSE_MS);
}

status.ok = !status.blocked && byId.size > 0;
status.totalReported = totalReported;
status.count = byId.size;
status.complete = totalReported !== null ? byId.size >= totalReported : null;
status.stopReason = stopReason;
status.newEpisodeCount = [...byId.values()].filter(s => s.newEpisode).length;
status.freeCount = [...byId.values()].filter(s => s.free).length;
status.simulcastCount = [...byId.values()].filter(s => s.simulcast).length;

// Bestehenden Katalog NICHT anfassen, wenn dieser Lauf komplett geblockt ist
const hadPrevious = existsSync(CATALOG_FILE);
if (status.blocked && byId.size === 0) {
  writeFileSync(STATUS_FILE, JSON.stringify(status, null, 2));
  console.log("ADN-Katalog blockt auch aus der GitHub-Infrastruktur – Diagnose in adn_catalog_status.json, Katalogdatei unveraendert.");
} else if (byId.size === 0) {
  writeFileSync(STATUS_FILE, JSON.stringify(status, null, 2));
  console.log("Keine Titel extrahiert – nur Status geschrieben (Layout-Änderung? Parse pruefen).");
} else {
  // Prev-Diff für schnelle Auswertung (neu hinzu/verschwunden seit letztem Lauf)
  let added = null, removed = null;
  try {
    if (hadPrevious) {
      const prev = JSON.parse(readFileSync(CATALOG_FILE, "utf8"));
      const prevIds = new Set((prev.shows || []).map(s => s.id));
      const curIds = new Set(byId.keys());
      added = [...curIds].filter(id => !prevIds.has(id)).length;
      removed = [...prevIds].filter(id => !curIds.has(id)).length;
    }
  } catch { /* Diff optional */ }
  status.diff = hadPrevious ? { added, removed } : null;

  writeFileSync(STATUS_FILE, JSON.stringify(status, null, 2));
  writeFileSync(CATALOG_FILE, JSON.stringify({
    updated: status.updated,
    source: BASE_URL,
    total: totalReported,
    count: byId.size,
    complete: status.complete,
    shows: [...byId.values()].sort((a, b) => a.id - b.id),
  }, null, 2));
  console.log(`Katalog geschrieben: ${byId.size} Titel (total=${totalReported}, complete=${status.complete}, diff=${JSON.stringify(status.diff)}).`);
}
