#!/usr/bin/env node
// AniList-Dump: reichert mal_anime.json / mal_manga.json um AniList-Daten an.
// Läuft ohne Token (nur öffentliche Daten); mit ANILIST_TOKEN stabilere Limits.
// Keine Abhängigkeiten — Node >= 18 (global fetch).

import { readFileSync, writeFileSync } from 'node:fs';

const API = 'https://graphql.anilist.co';
const BATCH = 25;        // IDs pro Request (konservativ wegen Complexity-Budget)
const SLEEP_MS = 1200;    // ~50 req/min → weit unter dem 90er-Limit

const headers = {
  'Content-Type': 'application/json',
  ...(process.env.ANILIST_TOKEN
    ? { Authorization: `Bearer ${process.env.ANILIST_TOKEN}` }
    : {}),
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// --- 1) MAL-IDs aus den Exporten extrahieren (defensiv, mehrere Formate) ---
function extractIds(file) {
  const raw = JSON.parse(readFileSync(file, 'utf8'));
  const arr = Array.isArray(raw) ? raw : (raw.list ?? raw.data ?? []);
  return arr
    .map((e) => e?.node?.id ?? e?.id)
    .filter((id) => Number.isInteger(id) && id > 0);
}

const animeIds = extractIds('mal_anime.json');
const mangaIds = extractIds('mal_manga.json');
console.log(`Gefunden: ${animeIds.length} Anime-IDs, ${mangaIds.length} Manga/LN-IDs`);

// Fail-Fast: leere oder fehlende Exporte → Job abbrechen statt leeren Dump committen
if (!animeIds.length || !mangaIds.length) {
  throw new Error('Keine IDs gefunden – Export-Format oder Dateipfad prüfen!');
}

// --- 2) GraphQL-Batch-Query via Page.media(idMal_in) ---
// Wichtig: KEINE Media(idMal: …)-Aliasse! AniList antwortet mit HTTP 404 und
// nullt den GESAMTEN Request, sobald auch nur eine idMal kein Mapping hat.
// Page.media(idMal_in: …) liefert nur die Treffer — fehlende IDs landen
// anschließend sauber in "unmapped" statt den Job zu sprengen.
const FIELDS = `
  id
  idMal
  format
  isAdult
  status
  genres
  averageScore
  title { romaji english native }
  externalLinks { site url type }
  nextAiringEpisode { episode airingAt timeUntilAiring }
`;

async function callGraphQL(query, variables) {
  for (let attempt = 1; attempt <= 4; attempt++) {
    const res = await fetch(API, {
      method: 'POST',
      headers,
      body: JSON.stringify({ query, variables }),
    });
    if (res.status === 429) {
      const wait = Number(res.headers.get('retry-after') ?? 60);
      console.log(`\nRate limit – warte ${wait}s …`);
      await sleep(wait * 1000);
      continue;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const json = await res.json();
    if (json.errors) throw new Error('GraphQL-Fehler: ' + JSON.stringify(json.errors));
    return json.data;
  }
  throw new Error('GraphQL nach 4 Versuchen abgebrochen');
}

async function dumpAll(ids, type) {
  const entries = [];
  const missing = []; // MAL-IDs ohne AniList-Eintrag
  const query = `
    query ($ids: [Int], $type: MediaType) {
      Page(page: 1, perPage: ${2 * BATCH}) {
        media(idMal_in: $ids, type: $type) { ${FIELDS} }
      }
    }`;
  for (let i = 0; i < ids.length; i += BATCH) {
    const batch = ids.slice(i, i + BATCH);
    const data = await callGraphQL(query, { ids: batch, type });
    const found = data?.Page?.media ?? [];
    const foundMal = new Set(found.map((m) => m.idMal));
    // Treffer übernehmen (auch Mehrfach-Mappings, z.B. LN + "the Comic")
    for (const media of found) entries.push(media);
    // Anfrage-IDs ohne Treffer → unmapped
    for (const malId of batch) if (!foundMal.has(malId)) missing.push(malId);
    process.stdout.write(`\r${type}: ${Math.min(i + BATCH, ids.length)}/${ids.length} verarbeitet`);
    await sleep(SLEEP_MS);
  }
  console.log('');
  return { entries, missing };
}

const anime = await dumpAll(animeIds, 'ANIME');
const manga = await dumpAll(mangaIds, 'MANGA');

const dump = {
  generated_at: new Date().toISOString(),
  source: 'graphql.anilist.co (v2), Matching über idMal',
  anime: anime.entries,
  manga: manga.entries,
  unmapped: { anime: anime.missing, manga: manga.missing },
};

writeFileSync('anilist_dump.json', JSON.stringify(dump, null, 1));
console.log(
  `OK: ${anime.entries.length} Anime, ${manga.entries.length} Manga/LN – ` +
  `${anime.missing.length + manga.missing.length} ohne AniList-Mapping (siehe "unmapped").`
);