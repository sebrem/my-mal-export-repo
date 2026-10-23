// MAL × Streaming-Verfügbarkeit V7 – JustWatch-GraphQL (Pilot, Katalogstufe)
// Datenquelle: apis.justwatch.com/graphql (inoffiziell, country DE/language de).
// werstreamt.es wurde verworfen: api2.werstreamt.es leitet auf sich selbst weiter (301-Selbstloop,
// Cloudflare-Anti-Bot, ohne Cookie) – für Automation unbrauchbar; GitHub-Runner-Test 23.10.2026
// und Sandbox-Live-Trace bestätigen dasselbe Bild. JustWatch deckt denselben Zweck ab (DE-Verfügbarkeit).
// Fund ≠ Empfehlung: Output ist Kandidatenliste für den Verfügbarkeits-Rollcheck, KEINE
// Video-Ebenen-Verifikation.
// Aufruf im Repo-Root: node scripts/werstreamt_pilot.mjs
// Output: streaming_pilot.json (wird committet + als Artifact hochgeladen)

import fs from 'node:fs';

const JW = 'https://apis.justwatch.com/graphql';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
const FETCH_TIMEOUT_MS = 8000;
const RUN_DEADLINE_MS = 15 * 60 * 1000;
const RUN_START = Date.now();
function timeLeft() { return RUN_DEADLINE_MS - (Date.now() - RUN_START); }

// JustWatch-Provider-ShortNames -> Klassifizierung (3 Ebenen, Nutzer-Klarstellung 23.10.2026):
//  direct: ADN, Crunchyroll direkt, Netflix, Prime Video, Disney+, waipu, Nebula, YouTube Premium
//  channel: aniverse-Prime-Channel (zählt);  caveat: Crunchyroll-Prime-Channel (cra) = NICHT CR-direct
//  other:  legal in DE verfügbar, nicht abonniert (Signal fürs Re-Lizenz-Monitoring)
const PROVIDER_MAP = {
  nfx: { name: 'Netflix', tier: 'direct' },
  nfa: { name: 'Netflix (Standard with Ads)', tier: 'direct',
         note: 'Netflix-Werbe-Tarif – zählt als Netflix-direct (Abo vorhanden).' },
  amp: { name: 'Amazon Prime Video', tier: 'direct' },
  // WICHTIG (JW-Paketliste 23.10.2026): amz = "Amazon Video" (technicalName "amazon") = Kauf/Leihe
  // bzw. Channel-Kohorte, amp = "amazonprime" = Prime-Abo. Two-Car-Fehler im Pilot: amz wurde als
  // Prime-direct gewertet, obwohl der Titel (Nutzer-Verifikat) im aniverse-Channel läuft.
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
  // ava tauchte bei Two Car (aniverse-Channel, Nutzer-Verifikat) parallel zu amz auf watch.amazon.de
  // auf – sehr wahrscheinlich Prime-Channel-Paket (vermutlich aniverse), aber nicht per Paketliste
  // verifizierbar (packages(country:DE,platform:WEB) listet Channel-Pakete nicht).
  ava: { name: 'Prime-Channel (aniverse-Verdacht)', tier: 'channel_candidate',
         note: 'Katalogstufe unsicher – beim Fund Video-Ebene/Channel im Prime-Check verifizieren.' },
  cra: { name: 'Crunchyroll (Amazon-Channel)', tier: 'cr_channel_caveat',
         note: 'Crunchyroll-Prime-Channel: Katalog weicht von Crunchyroll direkt ab (Nutzer-Erfahrung).' },
  jyn: { name: 'Joyn (nicht abonniert)', tier: 'other',
         note: 'Echter Streaming-Dienst ohne Sebastians Abo -> Re-Lizenz-Monitoring-Signal.' },
  // Kauf-/Disc-Shops: legal, aber KEIN Streaming -> eigenes Tier, kein Re-Lizenz-Signal
  adb: { name: 'Amazon (Kauf)', tier: 'buy_physical' },
  thl: { name: 'Thalia (Disc)', tier: 'buy_physical' },
  jpc: { name: 'JPC (Disc)', tier: 'buy_physical' },
  bue: { name: 'Bücher.de (Disc)', tier: 'buy_physical' },
  zav: { name: 'Zavvi (Disc)', tier: 'buy_physical' },
  hgl: { name: 'Hugendubel (Disc)', tier: 'buy_physical' },
};
function classifyPackage(shortName) {
  const p = PROVIDER_MAP[shortName];
  if (p) return { provider: shortName, name: p.name, tier: p.tier, note: p.note || null };
  return { provider: shortName, name: null, tier: 'other', note: 'unmapped shortName – Map nach erstem Lauf verfeinern' };
}

const PILOT_QUEUE = [
  { key: 'harukana_receive', aliases: ['Harukana Receive', 'はるかなレシーブ'], expected: 'hit' },
  { key: 'sousou_no_frieren', aliases: ['Sousou no Frieren', 'Frieren', 'Frieren: Beyond Journey\'s End', '葬送のフリーレン'], expected: 'hit' },
  { key: 'kusuriya', aliases: ['Kusuriya no Hitorigoto', 'The Apothecary Diaries', '薬屋のひとりごと'], expected: 'hit' },
  { key: 'senryuu_shoujo', aliases: ['Senryuu Shoujo', 'Senryu Girl', 'Senryuu Girl', '川柳少女'], expected: 'miss' },
  { key: 'anima_yell', aliases: ['Anima Yell', 'Anima Yell!', 'アニマエイル'], expected: 'miss' },
  { key: 'sakura_quest', aliases: ['Sakura Quest', 'サクラクエスト'], expected: 'miss' },
  { key: 'two_car', aliases: ['Two Car', 'つうかあ'], expected: 'miss' },
  { key: 'www_wagnaria', aliases: ['WWW.WORKING!!', 'WWW.WAGNARIA!!'], expected: 'miss' },
  { key: 'yuru_camp', aliases: ['Yuru Camp', 'Laid-Back Camp', 'ゆるキャン△'], expected: 'probe' },
  { key: 'non_non_biyori', aliases: ['Non Non Biyori', 'のんのんびより'], expected: 'miss' },
];

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
function loadCandidates() {
  const malRaw = JSON.parse(fs.readFileSync('mal_anime.json', 'utf8'));
  const mal = Array.isArray(malRaw) ? malRaw : (Array.isArray(malRaw?.list) ? malRaw.list : []);
  const alRaw = fs.existsSync('anilist_dump.json')
    ? JSON.parse(fs.readFileSync('anilist_dump.json', 'utf8')) : null;
  const anilist = Array.isArray(alRaw) ? alRaw
    : (Array.isArray(alRaw?.anime) ? alRaw.anime
    : (Array.isArray(alRaw?.data) ? alRaw.data : []));
  if (mal.length === 0) throw new Error('mal_anime.json: keine Liste gefunden (erwartet: .list[])');

  const aliasIndex = new Map();
  for (const rec of anilist) {
    const idMal = rec?.idMal ?? rec?.media?.idMal ?? rec?.mal_id;
    if (!idMal) continue;
    const t = rec?.title ?? rec?.media?.title ?? {};
    const titles = [t?.romaji, t?.english, t?.native,
                    ...(Array.isArray(rec?.synonyms) ? rec.synonyms : [])].filter(Boolean);
    const set = aliasIndex.get(idMal) || new Set();
    titles.forEach(x => set.add(String(x).toLowerCase()));
    aliasIndex.set(idMal, set);
  }

  const active = mal.filter(e => {
    const st = e?.list_status?.status ?? e?.status ?? '';
    return !['dropped', 'completed'].includes(String(st).toLowerCase());
  });

  const matched = PILOT_QUEUE.map(q => {
    const want = q.aliases.map(a => a.toLowerCase());
    const entry = active.find(e => {
      const id = e.mal_id ?? e.id;
      const own = String(e.title ?? e.name ?? '').toLowerCase();
      const aliases = aliasIndex.get(id);
      return want.some(w => own === w || own.includes(w) || (aliases && aliases.has(w)));
    });
    return { ...q, mal: entry ? { mal_id: entry.mal_id ?? entry.id, title: entry.title,
            status: entry.list_status?.status ?? entry.status } : null };
  });
  return { matched, activeCount: active.length, aliasIndex };
}

// --- Phase 2: JustWatch-Suche + Offers -----------------------------------------
// JustWatch DE liefert lokalisierte Titel ("Frieren - Nach dem Ende der Reise") und
// Schreibvarianten ("Senryuu Girl" vs. "Senryu Girl"). Matching deshalb 2-stufig:
//  a) normalisierte Gleichheit gegen Alias-Set (MAL-Titel + Queue-Aliase + AniList-Aliase)
//  b) Token-Überlappung: mind. 1 gemeinsames Token mit >=7 Zeichen (>=5 wäre zu locker:
//     "quest" matchte fälschlich "Galaxy Quest" – Pilot-Befund 23.10.2026)
function norm(s) { return String(s || '').toLowerCase().replace(/[^a-z0-9äöüß]/g, ''); }
function tokens(s) {
  return String(s || '').toLowerCase().replace(/[^a-z0-9äöüß ]/g, ' ').split(/\s+/).filter(t => t.length >= 5);
}
// liefert 'exact' | 'token' | null
function titleMatchConf(jwTitle, wantedNorm, wantedTokens) {
  const jn = norm(jwTitle);
  if (wantedNorm.has(jn)) return 'exact';
  const jt = new Set(tokens(jwTitle));
  for (const t of wantedTokens) if (jt.has(t) && t.length >= 7) return 'token';
  return null;
}

async function searchJustWatch(query, first = 5) {
  const r = await gql(`query { searchTitles(source: "web", country: DE, language: de, first: ${first}, ` +
    `filter: {searchQuery: ${JSON.stringify(query)}}) { edges { node { id objectId objectType ` +
    `content(country: DE, language: de) { title fullPath } } } } }`);
  const edges = r?.data?.data?.searchTitles?.edges;
  return { status: r.status, edges: Array.isArray(edges) ? edges.map(e => e.node) : null, raw: r.text.slice(0, 500) };
}

async function offersJustWatch(nodeId) {
  const r = await gql(`query { node(id: ${JSON.stringify(nodeId)}) { ... on MovieOrShow { id ` +
    `offers(country: DE, platform: WEB) { package { shortName } standardWebURL } } } }`);
  const offers = r?.data?.data?.node?.offers;
  return { status: r.status, offers: Array.isArray(offers) ? offers : null, raw: r.text.slice(0, 500) };
}

// --- Main ----------------------------------------------------------------------
async function main() {
  const out = { run: new Date().toISOString(),
                source: 'apis.justwatch.com/graphql (DE, Katalogstufe)',
                disclaimer: 'Fund != Empfehlung; Katalogstufe, keine Video-Ebenen-Verifikation. ' +
                            'Treffer-Tiers: hit_direct (Abo-Dienst direkt) > hit_channel (aniverse-Prime-Channel) > ' +
                            'hit_cr_channel_caveat (nicht CR-direct!) ; de_legal_only = legal in DE verfügbar, ' +
                            'aber nicht auf eigenen Diensten (Re-Lizenz-Monitoring-Signal). ' +
                            'werstreamt.es verworfen: 301-Selbstloop (Anti-Bot), für Automation unbrauchbar.',
                results: [], unmapped_providers: [] };

  const { matched, activeCount, aliasIndex } = loadCandidates();
  out.active_universe = { status_filter: 'alles außer dropped/completed', count: activeCount };
  out.queue_match = matched.map(m => ({ key: m.key, expected: m.expected, mal: m.mal }));

  for (const q of matched) {
    if (timeLeft() < 120000) { out.abort_reason = 'Selbst-Deadline vor ' + q.key + ' – Teil-Ergebnis gesichert.'; break; }
    let hit = null, errors = [];
    // Alias-Set fürs Matching: MAL-Titel + Queue-Aliase + AniList-Aliase der MAL-ID
    const wantedNorm = new Set();
    const wantedTokens = new Set();
    const addWanted = (s) => { if (s) { wantedNorm.add(norm(s)); tokens(s).forEach(t => wantedTokens.add(t)); } };
    addWanted(q.mal?.title);
    q.aliases.forEach(addWanted);
    if (q.mal?.mal_id && aliasIndex) for (const a of (aliasIndex.get(q.mal.mal_id) || [])) addWanted(a);
    const searchAliases = [q.mal?.title, ...q.aliases].filter(Boolean).slice(0, 3);
    for (const alias of searchAliases) {
      try {
        const s = await searchJustWatch(alias);
        if (!s.edges || s.edges.length === 0) continue;
        // 1) exakte/token-basierte Matches (sicher). 2) Fallback: Top-1-Treffer, wenn SHOW
        // (kein Film wie "Galaxy Quest") – als match_confidence 'low' gekennzeichnet, manuell prüfen.
        const withConf = s.edges.map(n => ({ n, conf: titleMatchConf(n?.content?.title, wantedNorm, wantedTokens) }));
        const matchedTuples = withConf.filter(x => x.conf);
        let nodes = matchedTuples.map(x => x.n);
        let matchConfidence = matchedTuples.length ? matchedTuples[0].conf : null;
        if (nodes.length === 0 && s.edges[0] && s.edges[0].objectType === 'SHOW') {
          nodes = [s.edges[0]];
          matchConfidence = 'low'; // Top-1-SHOW-Fallback: dt. lokalisierte Titel ("Die Tagebücher der Apothekerin") sind per Token nicht matchbar
        }
        if (nodes.length === 0) { errors.push(`${alias}: JW-Treffer ohne Titel-Match (${s.edges.map(n=>n.content?.title).slice(0,3).join(', ')})`); continue; }
        // Es koennen mehrere Knoten matchen (Film + Serie, EN- + DE-Eintrag): den mit
        // tatsaechlichen Offers bevorzugen (max. 2 Kandidaten anfragen), sonst den ersten.
        let node = null, o = null;
        for (const cand of nodes.slice(0, 2)) {
          const oo = await offersJustWatch(cand.id);
          if (oo.offers && oo.offers.length > 0) { node = cand; o = oo; break; }
          if (!node) { node = cand; o = oo; }
        }
        // Alle Match-Kandidaten ohne Offers? -> Top-1-SHOW (oft der dt. lokalisierte
        // Serien-Eintrag wie "Die Tagebücher der Apothekerin") mit niedriger Confidence versuchen.
        if (o.offers && o.offers.length === 0 && s.edges[0] && s.edges[0].objectType === 'SHOW'
            && s.edges[0].id !== node.id) {
          const oo = await offersJustWatch(s.edges[0].id);
          if (oo.offers && oo.offers.length > 0) {
            node = s.edges[0];
            matchConfidence = 'low';
            o = oo;
          }
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
        const other = offers.filter(x => x.tier === 'other');
        const buy = offers.filter(x => x.tier === 'buy_physical');
        const streaming = offers.filter(x => x.tier !== 'buy_physical');
        hit = { searched_as: alias, jw_id: node.id, jw_title: node.content?.title,
                jw_path: node.content?.fullPath || null,
                match_confidence: matchConfidence,
                offers_classified: offers,
                my_direct_hits: direct, my_channel_hits: channel, channel_candidates: channelCand,
                crunchyroll_channel_caveat: crCaveat,
                legal_de_other_providers: other,
                buy_physical_offers: buy,
                de_legal_availability: streaming.length > 0 };
        break;
      } catch (e) { errors.push(`${alias}: ${String(e)}`); }
    }
    out.results.push({ key: q.key, expected: q.expected, mal: q.mal, result: hit, errors });
    fs.writeFileSync('streaming_pilot.json', JSON.stringify(out, null, 2)); // inkrementell
    await new Promise(r2 => setTimeout(r2, 700)); // Rate-Limit-Respekt
  }

  out.verdict = out.results.map(r => ({
    key: r.key, expected: r.expected,
    observed: r.result ? (r.result.my_direct_hits.length ? 'hit_direct'
      : (r.result.my_channel_hits.length ? 'hit_channel'
      : (r.result.channel_candidates.length ? 'hit_channel_candidate'
      : (r.result.crunchyroll_channel_caveat.length ? 'hit_cr_channel_caveat'
      : (r.result.legal_de_other_providers.length ? 'de_legal_only' : 'no_hit'))))) : 'no_match',
  }));
  out.unmapped_providers = [...new Set(out.unmapped_providers)];
  fs.writeFileSync('streaming_pilot.json', JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out.verdict, null, 2));
  console.log('Unmapped ShortNames:', JSON.stringify(out.unmapped_providers));
  console.log('Geschrieben: streaming_pilot.json');
}

main().catch(e => { console.error('Pilot fehlgeschlagen:', e); process.exit(1); });
