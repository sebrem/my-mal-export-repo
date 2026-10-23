// WerStreamt.es Pilot – MAL-Anime × Streaming-Verfügbarkeit (Katalogstufe)
// Zweck: 1) Endpoint-Discovery auf api2.werstreamt.es, 2) Pilot-Abgleich 10 Titel
//        (Positive/Negative-Kontrollen aus dem Streaming-Monitoring), 3) Fund ≠ Empfehlung:
//        Output ist Kandidatenliste für den Verfügbarkeits-Rollcheck, KEINE Video-Ebene-Verifikation.
// Aufruf im Repo-Root: node scripts/werstreamt_pilot.mjs
// Output: streaming_pilot.json (Repo-Root; wird als Artifact hochgeladen)

import fs from 'node:fs';
import path from 'node:path';

const API = 'https://api2.werstreamt.es';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
const FETCH_TIMEOUT_MS = 8000;          // kurze Timeouts: hängende Verbindungen sollen den Lauf nicht fransen
const RUN_DEADLINE_MS = 15 * 60 * 1000; // Selbst-Deadline UNTER dem 20-Min-Job-Timeout -> Artifact wird sicher geschrieben
const RUN_START = Date.now();
function timeLeft() { return RUN_DEADLINE_MS - (Date.now() - RUN_START); }
const HDRS = {
  'User-Agent': UA,
  'Accept': 'application/json, text/plain, */*',
  'Accept-Language': 'de-DE,de;q=0.9,en;q=0.8',
  'Referer': 'https://www.werstreamt.es/',
  'Origin': 'https://www.werstreamt.es',
};

// Provider-Klassifizierung in 3 Ebenen (Nutzer-Klarstellung 23.10.2026):
//  1. DIRECT:  direkt abonnierte Dienste (ADN, Crunchyroll direkt, Netflix, Prime Video,
//              Disney+, waipu.tv, Nebula, YouTube Premium)
//  2. CHANNEL: Prime-Video-Channel, den Sebastian hat (nur aniverse). Crunchyroll-Prime-Channel
//              gilt NICHT als Crunchyroll-direct (Katalog weicht ab – Nutzer-Erfahrung).
//  3. Sonstige: nicht abonniert, aber "legal in DE verfügbar" – eigenständiges Signal
//              fürs Re-Lizenz-Monitoring (DE-Lizenz vorhanden erhöht Chance auf Wechsel zu Sebastians Diensten).
// werstreamt-Provider-Titel werden beim Pilot-Lauf gedumpt; diese Maps danach verifizieren.
const MY_DIRECT = [
  'netflix', 'amazon prime video', 'prime video', 'disney', 'crunchyroll',
  'adn', 'animation digital network', 'waipu', 'nebula', 'youtube',
];
const MY_CHANNELS = ['aniverse'];

// Pilot-Queue: Kontrollen aus knowledge/anime-manga/streaming-monitoring.md (Stand 23.10.2026)
// expected: 'hit' = auf Sebastians Diensten verfügbar erwartet, 'miss' = nicht verfügbar erwartet,
//           'probe' = offen, Diagnose-Titel.
const PILOT_QUEUE = [
  { key: 'harukana_receive', aliases: ['Harukana Receive', 'はるかなレシーブ'], expected: 'hit' },      // CR DE verifiziert (werstreamt stimmte schon einmal)
  { key: 'sousou_no_frieren', aliases: ['Sousou no Frieren', 'Frieren', '葬送のフリーレン'], expected: 'hit' },
  { key: 'kusuriya', aliases: ['Kusuriya no Hitorigoto', 'The Apothecary Diaries', '薬屋のひとりごと'], expected: 'hit' },
  { key: 'senryuu_shoujo', aliases: ['Senryuu Shoujo', 'Senryu Girl', '川柳少女'], expected: 'miss' },
  { key: 'anima_yell', aliases: ['Anima Yell', 'アニマエイル'], expected: 'miss' },
  { key: 'sakura_quest', aliases: ['Sakura Quest', 'サクラクエスト'], expected: 'miss' },
  { key: 'two_car', aliases: ['Two Car', 'つうかあ'], expected: 'miss' },
  { key: 'www_wagnaria', aliases: ['WWW.WAGNARIA!!', 'WWW.WORKING!!'], expected: 'miss' }, // Prime-Placeholder-Falle
  { key: 'yuru_camp', aliases: ['Yuru Camp', 'Laid-Back Camp', 'ゆるキャン△'], expected: 'probe' },
  { key: 'non_non_biyori', aliases: ['Non Non Biyori', 'のんのんびより'], expected: 'miss' }, // dokumentiert: DE nicht verfügbar, Manga-Lane
];

async function fetchSafe(url, init) {
  try {
    const res = await fetch(url, { ...init, signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      headers: { ...HDRS, ...(init?.headers || {}) } });
    const text = await res.text();
    return { status: res.status, ok: res.ok, text, headers: Object.fromEntries(res.headers) };
  } catch (e) {
    return { status: 0, ok: false, text: String(e), headers: {} };
  }
}

// --- Phase 1: Endpoint-Discovery --------------------------------------------
// Bekannte/historische Endpoint-Muster werden der Reihe nach geprobt; Roh-Antworten
// (gekürzt) landen im Diagnose-Block des Outputs. Legacy-Struktur (2018-Crawler):
//   /getStreamProviders -> {response:{Items:[{ID,Title}]}}
//   /lookup?type=Show&titles[]=<name>&year=<y> -> {response:[{ID,Title,...}]}
//   /getDetails?ContentID=<id> -> {response:{Streams:[{ProviderTitle,Offers,Link}]}}
async function discoverEndpoints(probeTitle) {
  const candidates = [
    { name: 'providers_legacy', url: `${API}/getStreamProviders` },
    { name: 'providers_alt', url: `${API}/providers` },
    { name: 'lookup_legacy', url: `${API}/lookup?type=Show&titles[]=${encodeURIComponent(probeTitle)}` },
    { name: 'search_underscore', url: `${API}/_search?q=${encodeURIComponent(probeTitle)}` },
    { name: 'search_plain', url: `${API}/search?q=${encodeURIComponent(probeTitle)}` },
    { name: 'search_titles', url: `${API}/titles/search?title=${encodeURIComponent(probeTitle)}` },
  ];
  const diag = [];
  for (const c of candidates) {
    const r = await fetchSafe(c.url);
    diag.push({ endpoint: c.name, url: c.url, status: r.status, contentType: r.headers['content-type'] || null,
                bodySnippet: r.text.slice(0, 800) });
    const d = diag[diag.length - 1];
    if (r.status === 429 || r.status === 403) d.note = 'Blocked/Rate-Limit – ggf. Delay/Headers anpassen (vgl. 418-Teapot-Lektion Manga+).';
  }
  return diag;
}

function tryParse(text) {
  try { return JSON.parse(text); } catch { return null; }
}

// --- Phase 2: MAL-Export laden, Kontrollen matchen ----------------------------
function loadCandidates() {
  const mal = JSON.parse(fs.readFileSync('mal_anime.json', 'utf8'));
  const anilist = fs.existsSync('anilist_dump.json')
    ? JSON.parse(fs.readFileSync('anilist_dump.json', 'utf8')) : [];
  // Alias-Index aus AniList-Dump (idMal -> Titel-Set)
  const aliasIndex = new Map();
  for (const rec of anilist) {
    const idMal = rec?.idMal ?? rec?.media?.idMal ?? rec?.mal_id;
    if (!idMal) continue;
    const m = rec?.media ?? rec;
    const titles = [m?.title?.romaji, m?.title?.english, m?.title?.native,
                    ...(Array.isArray(m?.synonyms) ? m.synonyms : [])].filter(Boolean);
    const set = aliasIndex.get(idMal) || new Set();
    titles.forEach(t => set.add(t.toLowerCase()));
    aliasIndex.set(idMal, set);
  }
  // Aktive Einträge: alles außer dropped/completed
  const active = mal.filter(e => !['dropped', 'completed'].includes(String(e.status ?? e.list_status).toLowerCase()));
  const matched = PILOT_QUEUE.map(q => {
    const want = q.aliases.map(a => a.toLowerCase());
    const entry = active.find(e => {
      const id = e.mal_id ?? e.id;
      const own = String(e.title ?? e.name ?? '').toLowerCase();
      const aliases = aliasIndex.get(id);
      return want.some(w => own === w || own.includes(w) || (aliases && aliases.has(w)));
    });
    return { ...q, mal: entry ? { mal_id: entry.mal_id ?? entry.id, title: entry.title ?? entry.name, status: entry.status ?? entry.list_status } : null };
  });
  return { matched, activeCount: active.length };
}

// --- Phase 3: Suche + Details je Queue-Titel ----------------------------------
async function queryTitle(alias) {
  const q = encodeURIComponent(alias);
  const attempts = [
    `${API}/lookup?type=Show&titles[]=${q}`,
    `${API}/_search?q=${q}`,
    `${API}/search?q=${q}`,
    `${API}/titles/search?title=${q}`,
  ];
  for (const url of attempts) {
    const r = await fetchSafe(url);
    const j = tryParse(r.text);
    const list = j?.response?.Items || j?.response || j?.results || j?.items || (Array.isArray(j) ? j : null);
    if (r.ok && Array.isArray(list) && list.length) {
      const first = list[0];
      const id = first?.ID ?? first?.id ?? first?.ContentID;
      if (id != null) return { via: url.split('?')[0], id, raw: first };
    }
  }
  return null;
}

async function queryDetails(id) {
  const attempts = [
    `${API}/getDetails?ContentID=${id}`,
    `${API}/titles/${id}`,
    `${API}/details?id=${id}`,
  ];
  for (const url of attempts) {
    const r = await fetchSafe(url);
    const j = tryParse(r.text);
    const d = j?.response || j;
    if (r.ok && d) return { via: url.split('?')[0], data: d };
  }
  return null;
}

function extractStreams(details) {
  const d = details?.data ?? details ?? {};
  const streams = d?.Streams || d?.streams || d?.Offers || d?.offers || [];
  return streams.map(s => {
    const offerList = s.Offers?.map(o => (o.OfferType ?? o.type)).join(',') || null;
    return {
      provider: s.ProviderTitle ?? s.provider_title ?? s.provider ?? null,
      offerType: offerList ?? (s.Type ?? s.type ?? null),
      link: s.Link ?? s.link ?? s.url ?? null,
    };
  });
}

// Provider einordnen: {tier: 'direct'|'channel'|'cr_channel_caveat'|'other', note}
function classifyProvider(provider) {
  if (!provider) return { tier: 'other', note: null };
  const p = String(provider).toLowerCase();
  const isChannel = p.includes('channel') || p.includes('kanal');
  // Crunchyroll über Prime-Channel: explizit NICHT als CR-direct werten
  if (p.includes('crunchyroll') && isChannel) {
    return { tier: 'cr_channel_caveat', note: 'Crunchyroll-Prime-Channel: Katalog weicht von Crunchyroll direkt ab (Nutzer-Erfahrung) – nicht als CR-Direct-Treffer werten.' };
  }
  if (MY_CHANNELS.some(s => p.includes(s))) return { tier: 'channel', note: 'Prime-Video-Channel im eigenen Abo (aniverse).' };
  if (MY_DIRECT.some(s => p.includes(s))) return { tier: 'direct', note: null };
  return { tier: 'other', note: isChannel ? 'Prime-Channel ohne eigenes Abo' : null };
}

// --- Main ----------------------------------------------------------------------
async function main() {
  const out = { run: new Date().toISOString(), source: 'api2.werstreamt.es (Pilot, Katalogstufe)',
                disclaimer: 'Fund != Empfehlung; Katalogstufe, keine Video-Ebenen-Verifikation. ' +
                            'Treffer-Tiers: my_direct_hits (Abo-Dienste direkt) > my_channel_hits (aniverse-Prime-Channel) > ' +
                            'crunchyroll_channel_caveat (nicht CR-direct!) ; legal_de_other_providers = legal in DE verfügbar, ' +
                            'aber nicht abonniert (Signal fürs Re-Lizenz-Monitoring).',
                diagnostics: {}, providers_raw: null, results: [] };

  const { matched, activeCount } = loadCandidates();
  out.active_universe = { status_filter: 'alles außer dropped/completed', count: activeCount };
  out.queue_match = matched.map(m => ({ key: m.key, expected: m.expected, mal: m.mal }));

  const probeAlias = matched.find(m => m.mal)?.aliases[0] || 'Frieren';
  out.diagnostics.endpoint_probe = await discoverEndpoints(probeAlias);

  // Diagnose-First: Wenn KEIN Search-Endpoint antwortet, sind alle Titel-Queries
  // zum Scheitern verurteilt -> Diagnose schreiben und sauber beenden (kein 20-Min-Timeout mehr).
  const workingSearch = out.diagnostics.endpoint_probe.filter(d =>
    d.status === 200 && !/^\s*</.test(d.bodySnippet)); // HTML-Antwort (Block/403-Seite) zählt nicht
  if (workingSearch.length === 0) {
    out.abort_reason = 'Kein funktionsfähiger Search-Endpoint gefunden – Diagnose-Modus, Titel-Queries übersprungen. ' +
                       'Nächste Schritte: bodySnippets prüfen (HTML-Blockseite? anderer Pfad? JustWatch-Fallback erwägen).';
    fs.writeFileSync('streaming_pilot.json', JSON.stringify(out, null, 2));
    console.log(out.abort_reason);
    console.log('Geschrieben: streaming_pilot.json (nur Diagnose)');
    return;
  }

  // Provider-Liste roh dumpen (für Map-Verifizierung der MY_DIRECT-/MY_CHANNELS-Namen)
  for (const u of [`${API}/getStreamProviders`, `${API}/providers`]) {
    const r = await fetchSafe(u);
    const j = tryParse(r.text);
    const items = j?.response?.Items || j?.response || (Array.isArray(j) ? j : null);
    if (r.ok && Array.isArray(items)) { out.providers_raw = items.map(p => p.Title ?? p.title ?? p); break; }
  }

  for (const q of matched) {
    if (timeLeft() < 120000) { // unter 2 Min Restbudget: lieber sauber finishen als vom Job kassiert werden
      out.abort_reason = 'Selbst-Deadline vor Titel ' + q.key + ' erreicht – Teil-Ergebnis wird gesichert.';
      break;
    }
    // max. 2 Such-Aliase (JP-Export-Titel + kürzester EN-Alias), um den Lauf kurz zu halten
    const searchAliases = [q.mal?.title, q.aliases[0]].filter(Boolean).slice(0, 2);
    let hit = null, errors = [];
    for (const alias of searchAliases) {
      try {
        const found = await queryTitle(alias);
        if (!found) continue;
        const det = await queryDetails(found.id);
        const streams = det ? extractStreams(det) : [];
        const classified = streams.map(s => ({ provider: s.provider, offerType: s.offerType, link: s.link,
                                                ...classifyProvider(s.provider) }));
        const direct = classified.filter(s => s.tier === 'direct');
        const channel = classified.filter(s => s.tier === 'channel');
        const crChannel = classified.filter(s => s.tier === 'cr_channel_caveat');
        const otherLegal = classified.filter(s => s.tier === 'other');
        hit = { searched_as: alias, wses_id: found.id, wses_title: found.raw?.Title ?? found.raw?.title ?? null,
                providers_classified: classified,
                my_direct_hits: direct, my_channel_hits: channel, crunchyroll_channel_caveat: crChannel,
                legal_de_other_providers: otherLegal.map(s => s.provider),
                de_legal_availability: classified.length > 0 };
        break;
      } catch (e) { errors.push(`${alias}: ${String(e)}`); }
    }
    out.results.push({ key: q.key, expected: q.expected, mal: q.mal, result: hit, errors });
    fs.writeFileSync('streaming_pilot.json', JSON.stringify(out, null, 2)); // inkrementell: Stand geht auch bei Abbruch nicht verloren
    await new Promise(r2 => setTimeout(r2, 1000)); // Rate-Limit-Respekt
  }

  const verdict = out.results.map(r => ({
    key: r.key, expected: r.expected,
    observed: r.result ? (r.result.my_direct_hits.length ? 'hit_direct'
      : (r.result.my_channel_hits.length ? 'hit_channel'
      : (r.result.crunchyroll_channel_caveat.length ? 'hit_cr_channel_caveat'
      : (r.result.legal_de_other_providers.length ? 'de_legal_only' : 'no_hit')))) : 'no_match',
  }));
  out.verdict = verdict;
  fs.writeFileSync('streaming_pilot.json', JSON.stringify(out, null, 2));
  console.log(JSON.stringify(verdict, null, 2));
  console.log('Geschrieben: streaming_pilot.json');
}

main().catch(e => { console.error('Pilot fehlgeschlagen:', e); process.exit(1); });
