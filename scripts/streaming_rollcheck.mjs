// MAL x Streaming-Verfügbarkeit ROLLCHECK V8 – JustWatch-GraphQL (Katalogstufe)
// Basiert auf werstreamt_pilot.mjs V7.1 (Pilot validiert 23.10.2026, alle Kontrollen korrekt).
// Unterschied zum Pilot: verarbeitet das GESAMTE aktive Universum (alles außer dropped/completed,
// aktuell 830 Titel) in Chunks (Standard 45 Titel/Lauf) mit persistiertem Fortschritt.
//   - streaming_state.json : { processed: [mal_id], cycle, hits: { mal_id: {...} } }
//     -> wird committet; jedes neue Kommando läuft mit dem gespeicherten Fortschritt weiter.
//   - streaming_rollcheck.json : Ergebnis des aktuellen Chunks + kumulierte Treffer des Cycles.
// Fund != Empfehlung: Kandidatenliste für den Verfügbarkeits-Rollcheck, KEINE Video-Ebenen-Verifikation.
// Aufruf im Repo-Root: node scripts/streaming_rollcheck.mjs

import fs from 'node:fs';

const JW = 'https://apis.justwatch.com/graphql';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
const FETCH_TIMEOUT_MS = 8000;
const RUN_DEADLINE_MS = 15 * 60 * 1000; // unter dem 20-Min-Job-Timeout
const RUN_START = Date.now();
function timeLeft() { return RUN_DEADLINE_MS - (Date.now() - RUN_START); }
const CHUNK_SIZE = Number(process.env.ROLLCHECK_CHUNK || '45');
const STATE_FILE = 'streaming_state.json';
const OUT_FILE = 'streaming_rollcheck.json';

// JustWatch-Provider-ShortNames -> Klassifizierung (verifiziert per packages(country:DE,platform:WEB)
// und Pilot-Läufen 23.10.2026; Channel-Pakete listet die Paketabfrage nicht -> ava = Verdacht).
const PROVIDER_MAP = {
  nfx: { name: 'Netflix', tier: 'direct' },
  nfa: { name: 'Netflix (Standard with Ads)', tier: 'direct',
         note: 'Netflix-Werbe-Tarif – zählt als Netflix-direct (Abo vorhanden).' },
  amp: { name: 'Amazon Prime Video', tier: 'direct' },
  // amz = "Amazon Video" (technicalName "amazon") = Kauf/Leihe bzw. Channel-Kohorte,
  // amp = "amazonprime" = Prime-Abo (Pilot-Befund Two Car 23.10.2026).
  amz: { name: 'Amazon Video (Kauf/Leihe bzw. Channel-Kohorte)', tier: 'other',
         note: 'Amazon Video = transaktional/Channel-Ablage, NICHT Prime-Abo-direct. Bei Prime-Fund: Video-Ebene klären.' },
  dis: { name: 'Disney+', tier: 'direct' },
  cru: { name: 'Crunchyroll', tier: 'direct' },
  adn: { name: 'ADN (Animation Digital Network)', tier: 'direct' },
  adn2: { name: 'ADN', tier: 'direct' },
  wai: { name: 'waipu.tv', tier: 'direct' },
  ytb: { name: 'YouTube', tier: 'direct' },
  ytp: { name: 'YouTube Premium', tier: 'direct' },
  neb: { name: 'Nebula', tier: 'direct' },
  ani: { name: 'aniverse (Prime Channel)', tier: 'channel' },
  // ava = aniverse: Nutzer-Prime-Check 23.10.2026 (AnoHana-Serie, Akatsuki no Yona,
  // Akuyaku Reijou Tensei Ojisan, Akane-banashi S1 – jeweils aniverse angezeigt)
  ava: { name: 'aniverse (Prime Channel)', tier: 'channel',
         note: 'Nutzer-verifiziert 23.10.2026 via Prime-Detailseiten.' },
  // pva = Prime-Kauf/Leihe-Variante (Yona-Check 23.10.: prime selbst + aniverse + kaufen/leihen
  // auf derselben GTI; amp=Prime-Abo, ava=aniverse, pva=kaufen/leihen)
  pva: { name: 'Prime Video (Kauf/Leihe, pva)', tier: 'buy_physical',
         note: 'Transaktional, kein Abo-Signal (Nutzer-Check Yona 23.10.2026).' },
  // ada = ADN-Prime-Channel (Akuyaku-Check 23.10.: aniverse, ADN, kaufen/leihen)
  ada: { name: 'ADN (Amazon-Channel)', tier: 'adn_channel_caveat',
         note: 'ADN-Prime-Channel: Katalogabweichung wie beim CR-Channel möglich (cra). Fund ≠ ADN-direct.' },
  // echte Streaming-Dienste ohne Sebastians Abo -> Re-Lizenz-Monitoring-Signal
  vik: { name: 'Rakuten Viki (nicht abonniert)', tier: 'other' },
  akp: { name: 'Akibapass (nicht abonniert)', tier: 'other' },
  cra: { name: 'Crunchyroll (Amazon-Channel)', tier: 'cr_channel_caveat',
         note: 'Crunchyroll-Prime-Channel: Katalog weicht von Crunchyroll direkt ab (Nutzer-Erfahrung).' },
  jyn: { name: 'Joyn (nicht abonniert)', tier: 'other',
         note: 'Echter Streaming-Dienst ohne Sebastians Abo -> Re-Lizenz-Monitoring-Signal.' },
  atp: { name: 'Apple TV+', tier: 'direct',
         note: 'Abo vorhanden (Nutzer 23.10.2026); kaum Anime, aber direkter Dienst.' },
  mxx: { name: 'HBO Max (nicht abonniert)', tier: 'other' },
  etv: { name: 'MagentaTV (nicht abonniert)', tier: 'other' },
  fmd: { name: 'freenet video (nicht abonniert)', tier: 'other' },
  itu: { name: 'Apple TV Store (Kauf/Leihe)', tier: 'buy_physical',
         note: 'itu = iTunes-Store, transaktional – NICHT Apple TV+ (atp).' },
  bzs: { name: 'Buyzoxs (Disc)', tier: 'buy_physical' },
  mds: { name: 'Maxdome Store (Kauf)', tier: 'buy_physical' },
  aho: { name: 'Prime-Channel (aho)', tier: 'channel_candidate',
         note: 'watch.amazon.de-Signatur wie ava – Video-Ebene im Prime-Check verifizieren.' },
  // Kauf-/Disc-Shops: legal, aber KEIN Streaming -> eigenes Tier, kein Re-Lizenz-Signal
  adb: { name: 'Amazon (Kauf)', tier: 'buy_physical' },
  thl: { name: 'Thalia (Disc)', tier: 'buy_physical' },
  jpc: { name: 'JPC (Disc)', tier: 'buy_physical' },
  bue: { name: 'Bücher.de (Disc)', tier: 'buy_physical' },
  zav: { name: 'Zavvi (Disc)', tier: 'buy_physical' },
  vdb: { name: 'Videobuster (Kauf/Leihe)', tier: 'buy_physical' },
  hgl: { name: 'Hugendubel (Disc)', tier: 'buy_physical' },
};
function classifyPackage(shortName) {
  const p = PROVIDER_MAP[shortName];
  if (p) return { provider: shortName, name: p.name, tier: p.tier, note: p.note || null };
  return { provider: shortName, name: null, tier: 'other', note: 'unmapped shortName – Map verfeinern' };
}

// Anime-typische Provider fuer den Fehltreffer-Schutz bei Token-Matches
const ANIME_PROVIDERS = new Set(['cru', 'cra', 'adn', 'adn2', 'ani', 'ava', 'ada', 'akp', 'vik']);

function causeStr(e) {
  const cause = e?.cause;
  return cause?.code ?? cause?.message ?? String(cause ?? 'unbekannt');
}

async function gql(query) {
  try {
    const res = await fetch(JW, { method: 'POST', signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json',
                 'User-Agent': UA, 'Accept-Language': 'de-DE,de;q=0.9' },
      body: JSON.stringify({ query }) });
    const text = await res.text();
    return { status: res.status, text, data: (() => { try { return JSON.parse(text); } catch { return null; } })() };
  } catch (e) {
    return { status: 0, text: `fetch failed | cause: ${causeStr(e)}`, data: null };
  }
}

// --- Phase 1: MAL-Export laden (Repo-Formate: mal_anime.json=.list[], anilist_dump.json=.anime[])
function loadUniverse() {
  const malRaw = JSON.parse(fs.readFileSync('mal_anime.json', 'utf8'));
  const mal = Array.isArray(malRaw) ? malRaw : (Array.isArray(malRaw?.list) ? malRaw.list : []);
  const alRaw = fs.existsSync('anilist_dump.json')
    ? JSON.parse(fs.readFileSync('anilist_dump.json', 'utf8')) : null;
  const anilist = Array.isArray(alRaw) ? alRaw
    : (Array.isArray(alRaw?.anime) ? alRaw.anime
    : (Array.isArray(alRaw?.data) ? alRaw.data : []));
  if (mal.length === 0) throw new Error('mal_anime.json: keine Liste gefunden (erwartet: .list[])');

  // AniList-Aliase (romaji/english/native/synonyms) je MAL-ID
  const aliasIndex = new Map();
  for (const rec of anilist) {
    const idMal = rec?.idMal ?? rec?.media?.idMal ?? rec?.mal_id;
    if (!idMal) continue;
    const t = rec?.title ?? rec?.media?.title ?? {};
    const titles = [t?.romaji, t?.english, t?.native,
                    ...(Array.isArray(rec?.synonyms) ? rec.synonyms : [])].filter(Boolean);
    const set = aliasIndex.get(idMal) || new Set();
    titles.forEach(x => set.add(String(x)));
    aliasIndex.set(idMal, set);
  }

  // aktives Universum: alles außer dropped/completed, stabile Reihenfolge (MAL-Export-Ordnung)
  const active = mal.filter(e => {
    const st = e?.list_status?.status ?? e?.status ?? '';
    return !['dropped', 'completed'].includes(String(st).toLowerCase());
  }).map(e => {
    const id = e.mal_id ?? e.id;
    const title = String(e.title ?? e.name ?? '');
    const aliases = [title, ...(aliasIndex.get(id) || [])];
    return { mal_id: id, title, status: e.list_status?.status ?? e.status, aliases };
  });
  return { active, activeCount: active.length };
}

// --- Phase 2: JustWatch-Suche + Offers (Matching wie Pilot V7.1, validiert 23.10.2026)
function norm(s) { return String(s || '').toLowerCase().replace(/[^a-z0-9äöüß]/g, ''); }
function tokens(s) {
  return String(s || '').toLowerCase().replace(/[^a-z0-9äöüß ]/g, ' ').split(/\s+/).filter(t => t.length >= 5);
}
function titleMatchConf(jwTitle, wantedNorm, wantedTokens) {
  const jn = norm(jwTitle);
  if (wantedNorm.has(jn)) return 'exact';
  const jt = new Set(tokens(jwTitle));
  for (const t of wantedTokens) if (jt.has(t) && t.length >= 7) return 'token';
  return null;
}

// JustWatch rate-limtet Runner-IPs gelegentlich mit 429 (Repo-Lauf 23.10.2026, Titel 43-45).
// Backoff-Retry: 2x mit wachsender Wartezeit; 429 im Such-Step ist die dominierende Fehlerart.
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
async function gqlWithBackoff(query, label) {
  const waits = [0, 30000, 60000];
  let r = null;
  for (let i = 0; i < waits.length; i++) {
    if (waits[i]) await sleep(waits[i]);
    r = await gql(query);
    if (r.status !== 429) return r;
  }
  return r; // letzter Versuch (429) – Aufrufer entscheidet über Abbruch
}

async function searchJustWatch(query, first = 5) {
  const r = await gqlWithBackoff(`query { searchTitles(source: "web", country: DE, language: de, first: ${first}, ` +
    `filter: {searchQuery: ${JSON.stringify(query)}}) { edges { node { id objectId objectType ` +
    `content(country: DE, language: de) { title fullPath } } } } }`);
  const edges = r?.data?.data?.searchTitles?.edges;
  return { status: r.status, edges: Array.isArray(edges) ? edges.map(e => e.node) : null, raw: r.text.slice(0, 500) };
}

async function offersJustWatch(nodeId) {
  const r = await gqlWithBackoff(`query { node(id: ${JSON.stringify(nodeId)}) { ... on MovieOrShow { id ` +
    `offers(country: DE, platform: WEB) { package { shortName } standardWebURL } } } }`);
  const offers = r?.data?.data?.node?.offers;
  return { status: r.status, offers: Array.isArray(offers) ? offers : null, raw: r.text.slice(0, 500) };
}

// --- Phase 3: Chunk-State -------------------------------------------------------
function loadState() {
  try {
    const s = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
    if (s && Array.isArray(s.processed) && typeof s.cycle === 'number') {
      s.hits = s.hits && typeof s.hits === 'object' ? s.hits : {};
      return s;
    }
  } catch {}
  return { processed: [], cycle: 1, cycle_started: new Date().toISOString(), hits: {} };
}
function saveState(state) { fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2)); }

// --- Main ----------------------------------------------------------------------
async function main() {
  const out = { run: new Date().toISOString(),
                source: 'apis.justwatch.com/graphql (DE, Katalogstufe)',
                chunk_size: CHUNK_SIZE,
                disclaimer: 'Fund != Empfehlung; Katalogstufe, keine Video-Ebenen-Verifikation. ' +
                            'Treffer-Tiers: hit_direct (Abo-Dienst direkt) > hit_channel (aniverse-Prime-Channel) > ' +
                            'hit_channel_candidate (Prime-Channel-Verdacht) > hit_cr_channel_caveat / ' +
                            'hit_adn_channel_caveat (Channel-Katalog weicht ab – nicht CR-/ADN-direct!) ; ' +
                            'de_legal_only = legal in DE verfügbar, aber nicht auf eigenen Diensten ' +
                            '(Re-Lizenz-Monitoring-Signal). buy_physical = Disc/Kauf, kein Streaming-Signal. ' +
                            'werstreamt.es verworfen: 301-Selbstloop (Anti-Bot), für Automation unbrauchbar.',
                unmapped_providers: [] };

  const { active, activeCount } = loadUniverse();
  const state = loadState();
  out.universe = { status_filter: 'alles außer dropped/completed', count: activeCount };

  // Chunk bestimmen: nächste CHUNK_SIZE aktiven Titel, die nicht im aktuellen Cycle liefen
  let queue = active.filter(t => !state.processed.includes(t.mal_id));
  let cycleCompleted = false;
  if (queue.length === 0) {
    // Cycle fertig -> neuen Cycle starten und von vorn
    cycleCompleted = true;
    state.processed = [];
    state.cycle = (state.cycle || 0) + 1;
    state.cycle_started = new Date().toISOString();
    state.hits = {};
    queue = active.slice(0, CHUNK_SIZE);
  }
  const chunk = queue.slice(0, CHUNK_SIZE);
  out.cycle = { number: state.cycle, started: state.cycle_started,
                processed_before: state.processed.length, universe: activeCount,
                cycle_completed_last_run: cycleCompleted };
  out.chunk = { count: chunk.length, first_mal_id: chunk[0]?.mal_id ?? null,
                last_mal_id: chunk[chunk.length - 1]?.mal_id ?? null };

  const chunkResults = [];
  for (const t of chunk) {
    if (timeLeft() < 120000) { out.abort_reason = `Selbst-Deadline vor mal_id ${t.mal_id} – Teil-Ergebnis gesichert.`; break; }
    let hit = null; const errors = [];
    const wantedNorm = new Set(); const wantedTokens = new Set();
    const addWanted = (s) => { if (s) { wantedNorm.add(norm(s)); tokens(s).forEach(x => wantedTokens.add(x)); } };
    t.aliases.forEach(addWanted);
    const searchAliases = [...new Set(t.aliases.filter(Boolean))].slice(0, 3);
    for (const alias of searchAliases) {
      try {
        const s = await searchJustWatch(alias);
        if (!s.edges) { errors.push(`${alias}: search ${s.status} ${s.raw}`); continue; }
        let node = null, o = null, matchConfidence = null;
        for (const cand of s.edges) {
          if (cand.objectType === 'MOVIE') continue; // nur Serien-Entries
          const mc = titleMatchConf(cand.content?.title, wantedNorm, wantedTokens);
          if (!mc) continue;
          const oo = await offersJustWatch(cand.id);
          // Fehltreffer-Schutz (Befund 23.10.2026: "Brothers Conflict" matchte
          // "Band of Brothers" via Token "brothers" -> Prime-direct-Fehlalarm):
          // Token-Matches (nicht exakt) akzeptieren nur Offers mit Anime-Provider.
          // Exakte Matches zaehlen immer. Bekannter Trade-off: ein Anime, der
          // NUR auf Prime-Abo laeuft und nur per Token matcht, wird uebersehen
          // (Low-Confidence-Verifizierung bleibt dem Video-Rollcheck).
          if (mc === 'token' && oo.offers && oo.offers.length > 0
              && !oo.offers.some(of => ANIME_PROVIDERS.has(of?.package?.shortName))) continue;
          if (oo.offers && oo.offers.length > 0) { node = cand; o = oo; matchConfidence = mc; break; }
          if (!node) { node = cand; o = oo; }
        }
        // Alle Match-Kandidaten ohne Offers? -> Top-1-SHOW-Fallback (dt. lokalisierte Einträge,
        // Pilot-Befund Kusuriya 23.10.2026) mit niedriger Confidence.
        if (o && o.offers && o.offers.length === 0 && s.edges[0] && s.edges[0].objectType === 'SHOW'
            && s.edges[0].id !== node?.id) {
          const oo = await offersJustWatch(s.edges[0].id);
          if (oo.offers && oo.offers.length > 0) { node = s.edges[0]; matchConfidence = 'low'; o = oo; }
        }
        if (!node) continue;
        const offers = (o.offers || []).map(of => {
          const c = classifyPackage(of?.package?.shortName);
          if (!c.name) out.unmapped_providers.push(of?.package?.shortName);
          return { ...c, url: of?.standardWebURL ?? null };
        });
        const direct = offers.filter(x => x.tier === 'direct');
        const channel = offers.filter(x => x.tier === 'channel');
        const channelCand = offers.filter(x => x.tier === 'channel_candidate');
        const crCaveat = offers.filter(x => x.tier === 'cr_channel_caveat');
        const adnCaveat = offers.filter(x => x.tier === 'adn_channel_caveat');
        const other = offers.filter(x => x.tier === 'other');
        const buy = offers.filter(x => x.tier === 'buy_physical');
        const streaming = offers.filter(x => x.tier !== 'buy_physical');
        const dedup = (arr) => { const seen = new Set(); return arr.filter(x => { const k = x.provider + '|' + x.url; if (seen.has(k)) return false; seen.add(k); return true; }); };
        const verdict = direct.length ? 'hit_direct'
          : channel.length ? 'hit_channel'
          : channelCand.length ? 'hit_channel_candidate'
          : crCaveat.length ? 'hit_cr_channel_caveat'
          : adnCaveat.length ? 'hit_adn_channel_caveat'
          : other.length ? 'de_legal_only'
          : (streaming.length === 0 && offers.length === 0 ? 'no_hit' : 'no_stream_hit');
        hit = { searched_as: alias, jw_id: node.id, jw_title: node.content?.title,
                jw_path: node.content?.fullPath || null,
                match_confidence: matchConfidence,
                verdict,
                my_direct_hits: dedup(direct), my_channel_hits: dedup(channel),
                channel_candidates: dedup(channelCand),
                crunchyroll_channel_caveat: dedup(crCaveat),
                adn_channel_caveat: dedup(adnCaveat),
                legal_de_other_providers: dedup(other),
                buy_physical_offers: dedup(buy),
                de_legal_availability: streaming.length > 0 };
        break;
      } catch (e) { errors.push(`${alias}: ${String(e)}`); }
    }
    // 429 auch nach Backoff-Retries -> Chunk hier abbrechen, Titel NICHT als processed
    // markieren (wird im nächsten Lauf wiederholt; Repo-Vorfall 23.10.2026).
    if (!hit && errors.some(e => e.includes('429'))) {
      out.abort_reason = `JustWatch-Rate-Limit (429) bei "${t.title}" trotz Backoff – Chunk abgebrochen, Titel wird im nächsten Lauf wiederholt.`;
      break;
    }
    const rec = { mal_id: t.mal_id, title: t.title, status: t.status,
                  result: hit || (errors.length ? null : { verdict: 'no_match' }),
                  errors };
    chunkResults.push(rec);
    state.processed.push(t.mal_id);
    // Treffer kumulieren (nur relevante Tiers; Overwrite je mal_id = jeweils neuester Stand)
    if (hit && hit.verdict !== 'no_hit' && hit.verdict !== 'no_match' && hit.verdict !== 'no_stream_hit') {
      state.hits[t.mal_id] = { title: t.title, mal_status: t.status, checked_at: out.run,
                               cycle: state.cycle, verdict: hit.verdict,
                               match_confidence: hit.match_confidence, jw_title: hit.jw_title,
                               my_direct_hits: hit.my_direct_hits, my_channel_hits: hit.my_channel_hits,
                               channel_candidates: hit.channel_candidates,
                               crunchyroll_channel_caveat: hit.crunchyroll_channel_caveat,
                               adn_channel_caveat: hit.adn_channel_caveat,
                               legal_de_other_providers: hit.legal_de_other_providers };
    } else if (state.hits[t.mal_id]) {
      state.hits[t.mal_id].checked_at = out.run; // no_hit: Timestamp aktualisieren, Eintrag behalten
    }
    fs.writeFileSync(OUT_FILE, JSON.stringify({ ...out, chunk_results: chunkResults,
      cycle_hits: state.hits }, null, 2)); // inkrementell
    await new Promise(r => setTimeout(r, 700)); // Rate-Limit-Respekt
  }

  out.chunk_results = chunkResults;
  out.cycle_hits_summary = Object.values(state.hits).reduce((acc, h) => { acc[h.verdict] = (acc[h.verdict] || 0) + 1; return acc; }, {});
  out.cycle_progress = { processed: state.processed.length, of: activeCount,
                         remaining: activeCount - state.processed.length,
                         eta_runs: Math.ceil((activeCount - state.processed.length) / CHUNK_SIZE) };
  out.unmapped_providers = [...new Set(out.unmapped_providers)];
  saveState(state);
  fs.writeFileSync(OUT_FILE, JSON.stringify({ ...out, cycle_hits: state.hits }, null, 2));
  console.log(JSON.stringify({ chunk: out.chunk, cycle: out.cycle.number,
    progress: out.cycle_progress, hits_summary: out.cycle_hits_summary,
    unmapped: out.unmapped_providers }, null, 2));
  console.log('Geschrieben:', OUT_FILE, '+', STATE_FILE);
}

await main();
