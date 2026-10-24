import fs from 'node:fs';

// Seiyuu & Themes Harvest (v3.0, 24.10.2026)
// Liest mal_anime.json, leitet fuer Score>=9-Titel ab:
//  - OP/ED-Interpreten (animethemes.moe: /search?q= fuer Slug-Matching; Website-Seite ist
//    React-SSR-HTML -> Token-Parser fuer den Theme-Stream, Stand 22.10.2026)
//  - japanische Hauptcast-Stimmen (AniList GraphQL, idMal-Matching)
// NEU v3.0 (24.10.2026): Lokale Mini-DB anime_db.json fuer ALLE Listentitel (nicht nur Favoriten):
//  - Synopse (description), Genres, Tags, Charakternamen+Rollen, Cover, Format, Jahr, Studio, Episoden
//  - Zweck: vage Inhaltsanfragen ("der mit dem Hasen") lokal statt per Websuche aufloesen
//  - Laeuft im selben Batch-Rhythmus (~500 Titel / 50er-Batches => ~11 Requests/Monat zusaetzlich)
// v1-Fixes: AniList 'perMedia' kennt Media.characters nicht -> page/perPage (HTTP 400);
//           animethemes-API /anime?filter[search][] blockiert -> /search + HTML-Parse;
//           UA 'curl/8.5.0' noetig (node-Default-UA: 403, eigene UA-Strings: 520).

const MIN_SCORE = 9;
const AL_ENDPOINT = 'https://graphql.anilist.co';
const AT_SEARCH = 'https://api.animethemes.moe/search?q=';
const AT_PAGE = 'https://animethemes.moe/anime/';
const SLEEP_MS = 700;
const UA = { 'User-Agent': 'curl/8.5.0' };

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, '');

const mal = JSON.parse(fs.readFileSync('mal_anime.json', 'utf8'));
const favs = (mal.list || []).filter(e => (e.list_status?.score ?? 0) >= MIN_SCORE);
console.log(`Favoriten (Score >= ${MIN_SCORE}): ${favs.length} Titel`);

// ---------- AniList: Anfrage-Header (authentifiziert, falls ANILIST_TOKEN gesetzt) ----------
function alHeaders() {
  const h = { 'Content-Type': 'application/json', Accept: 'application/json' };
  // Authentifizierter Zugang gegen AniList (ANILIST_TOKEN-Secret): erhoeht das
  // Rate-Limit von 30 auf 90 req/min und identifiziert uns gegenueber AniList,
  // die sich oeffentlich ueber Performance-Probleme durch viele API-Anfragen
  // beschwert hat. Ohne Token laeuft das Skript unauthentisiert weiter.
  if (process.env.ANILIST_TOKEN) h['Authorization'] = 'Bearer ' + process.env.ANILIST_TOKEN;
  return h;
}

// ---------- AniList: Lokale Mini-DB (ALLE Listentitel) ----------
const DB_QUERY = `query ($ids: [Int]) {
  Page(perPage: 50) {
    media(idMal_in: $ids, type: ANIME) {
      idMal
      title { romaji english native }
      description(asHtml: false)
      format
      episodes
      seasonYear
      averageScore
      genres
      tags { name rank }
      studios(isMain: true) { edges { node { name } } }
      coverImage { medium }
      characters(page: 1, perPage: 10, sort: [ROLE, RELEVANCE]) {
        edges {
          role
          node { name { full } }
        }
      }
    }
  }
}`;

async function fetchDb(idMalList) {
  const map = new Map();
  for (let i = 0; i < idMalList.length; i += 50) {
    const batch = idMalList.slice(i, i + 50);
    try {
      const res = await fetch(AL_ENDPOINT, {
        method: 'POST',
        headers: alHeaders(),
        body: JSON.stringify({ query: DB_QUERY, variables: { ids: batch } })
      });
      if (!res.ok) { console.warn(`AniList DB HTTP ${res.status} fuer Batch ${i}`); await sleep(3000); continue; }
      const json = await res.json();
      for (const m of json?.data?.Page?.media || []) if (m.idMal) map.set(m.idMal, m);
    } catch (err) {
      console.warn(`AniList DB Fehler Batch ${i}: ${err.message}`);
    }
    await sleep(1500);
  }
  return map;
}

function writeAnimeDb(dbMap) {
  const entries = [];
  for (const e of (mal.list || [])) {
    const id = e.id;
    const m = dbMap.get(id);
    if (!m) continue; // kein AniList-Treffer (z.B. Specials ohne Eintrag) -> nur.authoritative Treffer
    entries.push({
      id,
      title: e.title,
      anilist: {
        romaji: m.title?.romaji ?? null,
        english: m.title?.english ?? null,
        native: m.title?.native ?? null,
        description: m.description ?? null,
        format: m.format ?? null,
        episodes: m.episodes ?? null,
        seasonYear: m.seasonYear ?? null,
        averageScore: m.averageScore ?? null,
        genres: m.genres || [],
        tags: (m.tags || []).slice(0, 15).map(t => ({ name: t.name, rank: t.rank })),
        studios: (m.studios?.edges || []).map(s => s?.node?.name).filter(Boolean),
        cover: m.coverImage?.medium ?? null,
        characters: (m.characters?.edges || []).map(c => ({ name: c.node?.name?.full ?? null, role: c.role }))
      },
      list_status: e.list_status
    });
  }
  const out = {
    generated_at: new Date().toISOString(),
    source: 'mal_anime.json (alle Listentitel) x AniList GraphQL',
    purpose: 'Lokale Mini-DB fuer vage Inhaltsanfragen (Synopse/Tags/Charaktere statt Websuche)',
    count_titles: entries.length,
    titles: entries
  };
  fs.writeFileSync('anime_db.json', JSON.stringify(out, null, 2) + '\n');
  console.log(`OK: anime_db.json mit ${out.count_titles} Titeln geschrieben`);
}

// ---------- AniList: Hauptcast-Stimmen ----------
const CAST_QUERY = `query ($ids: [Int]) {
  Page(perPage: 50) {
    media(idMal_in: $ids, type: ANIME) {
      idMal
      title { romaji }
      characters(page: 1, perPage: 8, sort: [ROLE, RELEVANCE]) {
        edges {
          role
          node { name { full } }
          voiceActors(language: JAPANESE) { name { full } }
        }
      }
    }
  }
}`;

async function fetchCast(idMalList) {
  const map = new Map();
  for (let i = 0; i < idMalList.length; i += 50) {
    const batch = idMalList.slice(i, i + 50);
    try {
      const res = await fetch(AL_ENDPOINT, {
        method: 'POST',
        headers: alHeaders(),
        body: JSON.stringify({ query: CAST_QUERY, variables: { ids: batch } })
      });
      if (!res.ok) { console.warn(`AniList HTTP ${res.status} fuer Batch ${i}`); await sleep(3000); continue; }
      const json = await res.json();
      const media = json?.data?.Page?.media || [];
      for (const m of media) if (m.idMal) map.set(m.idMal, m);
    } catch (err) {
      console.warn(`AniList Fehler Batch ${i}: ${err.message}`);
    }
    await sleep(1500);
  }
  return map;
}

// ---------- animethemes.moe: Slug suchen ----------
// Season-Erkennung: "Season 2", "2nd Season", "Part 2", trailing "II/III/IV" (MAL nutzt roemische Ziffern)
function seasonInfo(rawName) {
  const s = (rawName || '').replace(/\(.*?\)/g, ' ');
  let num = null;
  const mArabic = s.match(/\b(?:season|part)\s*(\d+)\b/i) || s.match(/\b(\d+)(?:st|nd|rd|th)?\s*(?:season|part)\b/i);
  if (mArabic) num = +mArabic[1];
  const mRoman = s.match(/\b(II|III|IV|V|VI)\s*$/);
  if (num === null && mRoman) num = { II: 2, III: 3, IV: 4, V: 5, VI: 6 }[mRoman[1]];
  const base = s
    .replace(/\b\d*(?:st|nd|rd|th)?\s*(?:season|part)\b/gi, '')
    .replace(/\b(II|III|IV|V|VI)\s*$/, '')
    .replace(/\s+/g, ' ').trim();
  return { num, base };
}

async function findSlugOnce(query, ti) {
  const res = await fetch(AT_SEARCH + encodeURIComponent(query), { headers: { ...UA, Accept: 'application/json' } });
  if (!res.ok) { console.warn(`animethemes-Suche HTTP ${res.status} fuer ${query.slice(0, 40)}`); return null; }
  const json = await res.json();
  const candidates = json?.search?.anime || [];
  if (!candidates.length) return null;
  let best = null; let bestScore = -1;
  for (const c of candidates.slice(0, 10)) {
    const ci = seasonInfo(c.name);
    const tBase = norm(ti.base); const cBase = norm(ci.base);
    const baseEq = tBase === cBase;
    const baseInc = cBase.includes(tBase) || tBase.includes(cBase);
    let s = -1;
    if (norm(c.name) === norm(ti.title)) s = 100;
    else if (baseEq && ti.num !== null && ci.num === ti.num) s = 95;
    else if (baseEq && ti.num === null && ci.num === null) s = 90;
    else if (baseInc && ti.num !== null && ci.num === ti.num) s = 75;
    else if (baseInc && ti.num === null && ci.num === null) s = 70;
    else if (baseEq || baseInc) s = 30; // Season-Konflikt oder Basename-Match ohne Passung -> nur Fallback
    if (s > bestScore) { bestScore = s; best = c; }
  }
  return bestScore >= 70 ? best : null;
}

async function findSlug(title) {
  const ti = seasonInfo(title);
  ti.title = title;
  // Gestaffelte Queries: Volltitel -> 40 Zeichen -> 24 Zeichen (Riesen-Zitate schlagen in der Suche fehl)
  const queries = [title, title.slice(0, 40).trim(), title.slice(0, 24).trim()];
  for (const q of queries) {
    if (!q) continue;
    try {
      const hit = await findSlugOnce(q, ti);
      if (hit) return hit;
    } catch (err) {
      console.warn(`animethemes-Suche Fehler fuer ${q.slice(0, 40)}: ${err.message}`);
    }
    await sleep(400);
  }
  return null;
}

// ---------- animethemes.moe: Themes aus React-SSR-HTML (Token-Stream) ----------
const STOP_RE = /^v\d+$/;
const decode = (s) => s.replace(/&amp;/g, '&').replace(/&#x27;|&#39;/g, "'").replace(/&quot;/g, '"').trim();

async function fetchThemesBySlug(slug) {
  try {
    const res = await fetch(AT_PAGE + slug, { headers: { ...UA, Accept: 'text/html' } });
    if (!res.ok) { console.warn(`animethemes-Seite HTTP ${res.status} fuer ${slug}`); return null; }
    const html = await res.text();
    const body = html.replace(/<script[\s\S]*?<\/script>/g, '').replace(/<[^>]+>/g, '\u0001');
    const tokens = body.split('\u0001').map(decode).filter(Boolean);
    const themes = [];
    for (let i = 0; i < tokens.length; i++) {
      if (tokens[i] !== 'OP' && tokens[i] !== 'ED') continue;
      const type = tokens[i];
      let j = i + 1; let seq = null;
      if (/^\d+$/.test(tokens[j])) { seq = tokens[j]; j++; }
      const title = tokens[j]; j++;
      if (!title || tokens[j] !== 'by') continue; // kein Interpreten-Eintrag
      j++;
      const artists = [];
      while (j < tokens.length && !STOP_RE.test(tokens[j])) {
        artists.push(tokens[j]); j++;
      }
      themes.push({
        type: type + (seq ?? ''),
        title,
        artists,
        character_song: artists.some(a => /\(CV:/i.test(a))
      });
      i = j - 1;
    }
    return themes.length ? themes : null;
  } catch (err) {
    console.warn(`animethemes-Seite Fehler fuer ${slug}: ${err.message}`);
    return null;
  }
}

// ---------- 1) Lokale Mini-DB: ALLE Listentitel ----------
const allIds = (mal.list || []).map(e => e.id).filter(Boolean);
console.log(`Listentitel gesamt: ${allIds.length} (fuer anime_db.json)`);
const dbMap = await fetchDb(allIds);
writeAnimeDb(dbMap);

// ---------- 2) Themes + Seiyuu: Score >= 9-Titel (unchanged v2.2 logic) ----------
const castMap = await fetchCast(favs.map(e => e.id).filter(Boolean));

const perShow = [];
const artistFreq = new Map();
const seiyuuFreq = new Map();

for (const e of favs) {
  const title = e.title;
  const score = e.list_status.score;
  const match = await findSlug(title);
  const themes = match ? await fetchThemesBySlug(match.slug) : null;
  if (themes) {
    for (const t of themes) {
      for (const a of t.artists) {
        if (/\(CV:/i.test(a)) continue; // Charakter-/Seiyuu-Songs zaehlen nicht als Live-Act
        if (/^(more|\d+)$/.test(a)) continue; // Website-Abschneide-Artefakte ("N more") rausfiltern; per_show-Rohdaten unangetastet
        artistFreq.set(a, (artistFreq.get(a) || 0) + 1);
      }
    }
  }
  const cast = castMap.get(e.id);
  const seiyuu = [];
  if (cast) {
    for (const edge of cast.characters?.edges || []) {
      for (const va of edge.voiceActors || []) {
        const name = va?.name?.full;
        if (!name) continue;
        seiyuu.push({ character: edge.node?.name?.full ?? null, role: edge.role, seiyuu: name });
        seiyuuFreq.set(name, (seiyuuFreq.get(name) || 0) + 1);
      }
    }
  }
  perShow.push({ id: e.id, title, score, status: e.list_status.status, slug: match?.slug ?? null, themes, seiyuu });
  await sleep(SLEEP_MS);
}

const sortEntries = (m) => [...m.entries()].map(([name, count]) => ({ name, count })).sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));

const out = {
  generated_at: new Date().toISOString(),
  source: 'mal_anime.json (Score >= ' + MIN_SCORE + ') x animethemes.moe (search+page) x AniList GraphQL',
  min_score: MIN_SCORE,
  count_shows: perShow.length,
  themes_found: perShow.filter(s => s.themes).length,
  cast_found: perShow.filter(s => s.seiyuu.length).length,
  artist_frequency: sortEntries(artistFreq),
  seiyuu_frequency: sortEntries(seiyuuFreq),
  per_show: perShow
};

fs.writeFileSync('anime_themes_seiyuu.json', JSON.stringify(out, null, 2) + '\n');
console.log(`OK: ${out.themes_found}/${out.count_shows} Themes, ${out.cast_found}/${out.count_shows} Casts`);
console.log('Top-Artists:', out.artist_frequency.slice(0, 10).map(a => a.name + ' (' + a.count + ')').join(', '));
console.log('Top-Seiyuu:', out.seiyuu_frequency.slice(0, 10).map(s => s.name + ' (' + s.count + ')').join(', '));
