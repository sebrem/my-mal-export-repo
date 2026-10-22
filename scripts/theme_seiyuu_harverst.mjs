import fs from 'node:fs';

// Seiyuu & Themes Harvest
// Liest mal_anime.json, leitet fuer Score>=9-Titel ab:
//  - OP/ED-Interpreten (animethemes.moe API)
//  - japanische Hauptcast-Stimmen (AniList GraphQL, idMal-Matching)
// Schreibt anime_themes_seiyuu.json (per_show, artist_frequency, seiyuu_frequency).

const MIN_SCORE = 9;
const AL_ENDPOINT = 'https://graphql.anilist.co';
const AT_ENDPOINT = 'https://api.animethemes.moe';
const SLEEP_MS = 800;

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

const mal = JSON.parse(fs.readFileSync('mal_anime.json', 'utf8'));
const favs = (mal.list || []).filter(e => (e.list_status?.score ?? 0) >= MIN_SCORE);
console.log(`Favoriten (Score >= ${MIN_SCORE}): ${favs.length} Titel`);

// ---------- AniList: Hauptcast-Stimmen ----------
const CAST_QUERY = `query ($ids: [Int]) {
  Page(perPage: 50) {
    media(idMal_in: $ids, type: ANIME) {
      idMal
      title { romaji }
      characters(perMedia: 8, sort: [ROLE, RELEVANCE]) {
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
    const res = await fetch(AL_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ query: CAST_QUERY, variables: { ids: batch } })
    });
    if (!res.ok) { console.warn(`AniList HTTP ${res.status} fuer Batch ${i}`); await sleep(2000); continue; }
    const json = await res.json();
    const media = json?.data?.Page?.media || [];
    for (const m of media) if (m.idMal) map.set(m.idMal, m);
    await sleep(1200);
  }
  return map;
}

// ---------- animethemes.moe: OP/ED-Interpreten ----------
async function fetchThemes(title) {
  const params = new URLSearchParams();
  params.append('filter[search][]', title);
  params.append('include', 'themes.songs.artists');
  const url = AT_ENDPOINT + '/anime?' + params.toString();
  try {
    const res = await fetch(url, { headers: { Accept: 'application/json' } });
    if (!res.ok) { console.warn(`animethemes HTTP ${res.status} fuer ${title}`); return null; }
    const json = await res.json();
    const anime = (json.anime || [])[0];
    if (!anime) return null;
    const themes = (anime.themes || []).map(t => ({
      type: t.type,
      sequence: t.sequence ?? null,
      title: t.songs?.[0]?.title ?? null,
      artists: (t.songs?.[0]?.artists || []).map(a => a.name).filter(Boolean)
    }));
    return themes.length ? themes : null;
  } catch (err) {
    console.warn('animethemes Fehler fuer ' + title + ': ' + err.message);
    return null;
  }
}

const castMap = await fetchCast(favs.map(e => e.id).filter(Boolean));

const perShow = [];
const artistFreq = new Map();
const seiyuuFreq = new Map();

for (const e of favs) {
  const title = e.title;
  const score = e.list_status.score;
  const themes = await fetchThemes(title);
  if (themes) {
    for (const t of themes) {
      for (const a of t.artists) {
        if (/CV:|\(CV/i.test(a)) continue; // Charakter-Song-Eintraege zaehlen nicht als Live-Act
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
  perShow.push({ id: e.id, title, score, status: e.list_status.status, themes, seiyuu });
  await sleep(SLEEP_MS);
}

const sortEntries = (m) => [...m.entries()].map(([name, count]) => ({ name, count })).sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));

const out = {
  generated_at: new Date().toISOString(),
  source: 'mal_anime.json (Score >= ' + MIN_SCORE + ') x animethemes.moe x AniList GraphQL',
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
