import fs from 'node:fs';

// Staff-Harvest (v1.0, 02.11.2026)
// Liest mal_anime.json + mal_manga.json und erzeugt staff_harvest.json:
//  - ALLE bewerteten Titel (score > 0) gegen AniList GraphQL Staff:
//      MANGA: Story & Art / Story / Art / Original Story / Original Creator
//      ANIME: Director / Chief Director / Character Design / Original Character Design /
//             Original Creator / Original Story / Series Composition / Studios (isMain)
//  - Aggregierte Haeufigkeiten (analog act_freq im Themes-Harvest):
//      mangaka_freq, ln_author_freq, director_freq, chara_design_freq, series_comp_freq, studio_freq
//    je mit count / mean_score / hi_count (score>=9)
// Konzept-Basis: Autoren-/Staff-Analyse 02.11.2026 (Knowledge anime-manga/autoren-staff-analyse.md)
//  - Befund: Mangaka extrem breit gestreut (354 Autorinnen auf 348 Titel),
//    Studio-/Design-Signale nur als Sekundaer-Indikator. Dieses Harvest macht
//    den Anker-Check zum Regelfall statt Ad-hoc-Aktion.
// Technik: gleiche Batch-Logik wie theme_seiyuu_harvest.mjs (Page + idMal_in, 50er-Batches,
//  ANILIST_TOKEN fuer 90 req/min, 700 ms Sleep zwischen Requests).

const AL_ENDPOINT = 'https://graphql.anilist.co';
const SLEEP_MS = 700;
const MIN_SCORE = 0; // alle bewerteten Titel

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function alHeaders() {
  const h = { 'Content-Type': 'application/json', Accept: 'application/json' };
  if (process.env.ANILIST_TOKEN) h['Authorization'] = 'Bearer ' + process.env.ANILIST_TOKEN;
  return h;
}

const MANGA_QUERY = `query ($ids: [Int]) {
  Page(perPage: 50) {
    media(idMal_in: $ids, type: MANGA) {
      idMal
      title { romaji native }
      staff(perPage: 20, sort: RELEVANCE) { edges { role node { name { full } } } }
    }
  }
}`;

const ANIME_QUERY = `query ($ids: [Int]) {
  Page(perPage: 50) {
    media(idMal_in: $ids, type: ANIME) {
      idMal
      title { romaji native }
      studios(isMain: true) { edges { node { name } } }
      staff(perPage: 30, sort: RELEVANCE) { edges { role node { name { full } } } }
    }
  }
}`;

const MANGA_ROLES = /^(Story & Art|Story|Art|Original Story|Original Creator)$/;
const ANIME_ROLES = /^(Director|Chief Director|Character Design|Original Character Design|Original Creator|Original Story|Series Composition)$/;

async function fetchStaff(idMalList, query, label) {
  const map = new Map();
  for (let i = 0; i < idMalList.length; i += 50) {
    const batch = idMalList.slice(i, i + 50);
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        const res = await fetch(AL_ENDPOINT, {
          method: 'POST',
          headers: alHeaders(),
          body: JSON.stringify({ query, variables: { ids: batch } })
        });
        if (res.status === 429) {
          const wait = Number(res.headers.get('retry-after') || 60) * 1000;
          console.warn(`AniList 429 (${label} Batch ${i}) - warte ${wait} ms`);
          await sleep(wait);
          continue;
        }
        if (!res.ok) {
          console.warn(`AniList HTTP ${res.status} (${label} Batch ${i}), Versuch ${attempt}`);
          await sleep(3000);
          continue;
        }
        const json = await res.json();
        for (const m of json?.data?.Page?.media || []) {
          if (m.idMal && !map.has(m.idMal)) map.set(m.idMal, m);
        }
        break;
      } catch (err) {
        console.warn(`Fehler ${label} Batch ${i}, Versuch ${attempt}: ${err.message}`);
        await sleep(3000);
      }
    }
    await sleep(SLEEP_MS);
  }
  return map;
}

// Aggregation: name -> { count, mean_score, hi_count }
function aggregate(entries, staffPick) {
  const acc = new Map();
  for (const e of entries) {
    const names = new Set(staffPick(e).filter(Boolean));
    for (const n of names) {
      if (!acc.has(n)) acc.set(n, { count: 0, sum: 0, hi: 0 });
      const a = acc.get(n);
      a.count++; a.sum += e.score;
      if (e.score >= 9) a.hi++;
    }
  }
  return [...acc.entries()].map(([name, a]) => ({
    name,
    count: a.count,
    mean_score: Math.round((a.sum / a.count) * 100) / 100,
    hi_count: a.hi
  })).sort((x, y) => y.count - x.count || y.mean_score - x.mean_score);
}

// ---------- Manga ----------
const malManga = JSON.parse(fs.readFileSync('mal_manga.json', 'utf8'));
const mangaScored = (malManga.list || [])
  .filter(e => (e.list_status?.score ?? 0) > MIN_SCORE)
  .map(e => ({ id: e.id, title: e.title, score: e.list_status.score, status: e.list_status.status, volumes_read: e.list_status.num_volumes_read ?? 0 }));
console.log(`Bewertete Manga: ${mangaScored.length}`);
const mangaStaffMap = await fetchStaff(mangaScored.map(e => e.id), MANGA_QUERY, 'Manga');

const mangaEntries = [];
for (const e of mangaScored) {
  const m = mangaStaffMap.get(e.id);
  if (!m) continue;
  const mangaka = [];
  const origStory = [];
  for (const s of m.staff?.edges || []) {
    const name = s?.node?.name?.full;
    const role = s?.role || '';
    if (!name || !MANGA_ROLES.test(role)) continue;
    if (/^Story & Art$|^Story$|^Art$/.test(role)) mangaka.push({ name, role });
    else origStory.push({ name, role });
  }
  mangaEntries.push({
    id: e.id, title: e.title, score: e.score, status: e.status, volumes_read: e.volumes_read,
    anilist_title: m.title?.romaji ?? null,
    mangaka, original_story: origStory
  });
}
console.log(`Manga mit Staff-Treffer: ${mangaEntries.length}`);

// ---------- Anime ----------
const malAnime = JSON.parse(fs.readFileSync('mal_anime.json', 'utf8'));
const animeScored = (malAnime.list || [])
  .filter(e => (e.list_status?.score ?? 0) > MIN_SCORE)
  .map(e => ({ id: e.id, title: e.title, score: e.list_status.score, status: e.list_status.status, episodes_watched: e.list_status.num_episodes_watched ?? 0 }));
console.log(`Bewertete Anime: ${animeScored.length}`);
const animeStaffMap = await fetchStaff(animeScored.map(e => e.id), ANIME_QUERY, 'Anime');

const animeEntries = [];
for (const e of animeScored) {
  const m = animeStaffMap.get(e.id);
  if (!m) continue;
  const directors = [];
  const charaDesign = [];
  const origCreator = [];
  const seriesComp = [];
  for (const s of m.staff?.edges || []) {
    const name = s?.node?.name?.full;
    const role = s?.role || '';
    if (!name) continue;
    if (/^(Director|Chief Director)$/.test(role)) directors.push({ name, role });
    else if (/Character Design$/.test(role)) charaDesign.push({ name, role });
    else if (/^(Original Creator|Original Story)$/.test(role)) origCreator.push({ name, role });
    else if (role === 'Series Composition') seriesComp.push({ name, role });
  }
  animeEntries.push({
    id: e.id, title: e.title, score: e.score, status: e.status, episodes_watched: e.episodes_watched,
    anilist_title: m.title?.romaji ?? null,
    studios: (m.studios?.edges || []).map(s => s?.node?.name).filter(Boolean),
    directors, chara_design: charaDesign, original_creator: origCreator, series_composition: seriesComp
  });
}
console.log(`Anime mit Staff-Treffer: ${animeEntries.length}`);

// ---------- Aggregate (analog act_freq) ----------
const aggregates = {
  mangaka_freq: aggregate(mangaEntries, e => e.mangaka.map(s => s.name)),
  ln_author_freq: aggregate(mangaEntries, e => e.original_story.map(s => s.name)),
  anime_director_freq: aggregate(animeEntries, e => e.directors.map(s => s.name)),
  chara_design_freq: aggregate(animeEntries, e => e.chara_design.map(s => s.name)),
  original_creator_freq: aggregate(animeEntries, e => e.original_creator.map(s => s.name)),
  series_composition_freq: aggregate(animeEntries, e => e.series_composition.map(s => s.name)),
  studio_freq: aggregate(animeEntries, e => e.studios)
};

const out = {
  generated_at: new Date().toISOString(),
  source: 'mal_anime.json + mal_manga.json (alle bewerteten Titel) x AniList GraphQL Staff',
  purpose: 'Autoren-/Staff-Anker-Analyse: Mangaka, LN-Autoren, Regie, Charakterdesign, Series Composition, Studios je bewertetem Titel + Haeufigkeits-Aggregate',
  concept_reference: 'Knowledge anime-manga/autoren-staff-analyse.md (Erst-Analyse 02.11.2026: extreme Streuung, Studios/Design nur als Sekundaer-Indikator)',
  count_manga: mangaEntries.length,
  count_anime: animeEntries.length,
  aggregates,
  manga: mangaEntries,
  anime: animeEntries
};

fs.writeFileSync('staff_harvest.json', JSON.stringify(out, null, 2) + '\n');
console.log(`staff_harvest.json geschrieben: ${mangaEntries.length} Manga, ${animeEntries.length} Anime, ${Object.keys(aggregates).length} Aggregate`);
