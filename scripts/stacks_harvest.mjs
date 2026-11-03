// Interest-Stacks Harvest - Sebastians MAL Interest Stacks (public profile pages) abklopfen
// gegen den API-Bestand (mal_anime.json / mal_manga.json).
//
// Warum HTML statt API: MAL API v2 hat KEINE Stack-Endpoints (Stand 11/2026),
// Jikan hat Interest Stacks nur auf der v5-Roadmap. Die Stacks sind aber
// oeffentliche Seiten: /profile/{user}/stacks (Liste) und /stacks/{id} (Eintraege).
// Scraper ist bewusst defensiv: Rate-Limit, Retries, Sanity-Checks, Exit 1 bei
// Parse-Null-Ergebnis, damit ein MAL-Redesign laut faellt statt still zu liefern.

import { readFileSync, writeFileSync, existsSync } from "node:fs";

const MAL_USER = process.env.MAL_USER || "DevOpsElfMage";
const RATE_LIMIT_MS = 1100; // ~1 Request/s - Hoeflichkeit gegenueber MAL
const UA = "Mozilla/5.0 (X11; Linux x86_64) mal-export-bot/1.0 (stacks harvest)";

const summary = [];

function log(msg) {
  console.log(msg);
}

let lastFetch = 0;
async function politeFetch(url, { retries = 3 } = {}) {
  const wait = Math.max(0, RATE_LIMIT_MS - (Date.now() - lastFetch));
  if (wait > 0) await new Promise(r => setTimeout(r, wait));
  for (let attempt = 1; attempt <= retries; attempt++) {
    lastFetch = Date.now();
    const res = await fetch(url, { headers: { "User-Agent": UA } });
    if (res.ok) return await res.text();
    if (res.status === 404) return null;
    if (res.status === 429) {
      log(`  429 rate limited, warte 30s (Versuch ${attempt}/${retries}): ${url}`);
      await new Promise(r => setTimeout(r, 30000));
      continue;
    }
    log(`  HTTP ${res.status}, Retry in 10s (Versuch ${attempt}/${retries}): ${url}`);
    await new Promise(r => setTimeout(r, 10000));
  }
  throw new Error(`Fetch endgueltig fehlgeschlagen: ${url}`);
}

function decode(s) {
  return s
    .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"').replace(/&#0?39;/g, "'").replace(/&nbsp;/g, " ");
}

// Eintraege aus einer Stack-Detailseite ziehen:
// <a href=".../(anime|manga)/123/Slug">Titel</a> - pro Stack maximal 50 Eintraege, eine Seite.
function parseStackEntries(html) {
  const entries = [];
  const seen = new Set();
  // Sidebar ("More Top Anime" etc.) abschneiden: Hauptspalte endet vor "content-right"
  const cut = html.indexOf("content-right");
  const area = cut > 0 ? html.slice(0, cut) : html;
  const re = /<a[^>]+href="[^"]*?myanimelist\.net\/(anime|manga)\/(\d+)(?:\/[^"]*)?"[^>]*>([^<]+)<\/a>/gi;
  let m;
  while ((m = re.exec(area)) !== null) {
    const [, kind, idRaw, titleRaw] = m;
    const id = Number(idRaw);
    const title = decode(titleRaw).trim();
    if (!title || seen.has(kind + id)) continue;
    seen.add(kind + id);
    entries.push({ kind, id, title });
  }
  return entries;
}

// Stacks von der Profil-Seite (mit Pagination via ?page=N, bis keine neuen IDs mehr kommen).
async function harvestStackIds() {
  const ids = new Set();
  for (let page = 1; page <= 50; page++) {
    // Pagination der Profil-Stacks-Seite ist ?p=N (verifiziert 11/2026), ~10 Stacks pro Seite
    const url = `https://myanimelist.net/profile/${MAL_USER}/stacks?p=${page}`;
    const html = await politeFetch(url);
    if (html === null) break;
    const re = /myanimelist\.net\/stacks\/(\d+)/g;
    let m, before = ids.size;
    while ((m = re.exec(html)) !== null) ids.add(Number(m[1]));
    log(`Stacks-Seite ${page}: ${ids.size} IDs gesamt (+${ids.size - before})`);
    if (ids.size === before) break; // keine neuen -> Pagination-Ende
    if (!/page=\d+|More/.test(html) && ids.size > 0 && page > 1) {
      // Sicherheitshalber noch eine Seite weiter probieren; Abbruch oben bei 0 Neuzugang.
    }
  }
  return [...ids];
}

async function main() {
  // --- Bestand laden (API-Exporte aus dem Repo) ---
  const ownAnime = new Map();
  const ownManga = new Map();
  for (const [file, map] of [["mal_anime.json", ownAnime], ["mal_manga.json", ownManga]]) {
    if (!existsSync(file)) throw new Error(`${file} fehlt - MAL-Sync vorher laufen lassen.`);
    const data = JSON.parse(readFileSync(file, "utf8"));
    for (const e of data.list || []) {
      map.set(e.node?.id ?? e.id, {
        status: e.list_status?.status ?? null,
        score: e.list_status?.score ?? 0,
      });
    }
    log(`${file}: ${map.size} Bestandseintraege`);
  }

  // --- Stacks harvesten ---
  const stackIds = await harvestStackIds();
  if (stackIds.length === 0) {
    throw new Error("Keine Stack-IDs gefunden - MAL-Markup geaendert? Abbruch statt Muell-Commit.");
  }
  log(`\nHarveste ${stackIds.length} Stacks...`);

  const stacks = [];
  let failedStacks = 0;
  for (const [i, id] of stackIds.entries()) {
    const html = await politeFetch(`https://myanimelist.net/stacks/${id}`);
    if (html === null) { failedStacks++; continue; }
    // Titel: <h2 class="title"> (verifiziert), Fallback <title> ohne MAL-Suffix
    const titleM = html.match(/<h2 class="title">([^<]+)<\/h2>/i)
      || html.match(/<title>([^<]+?)\s*-\s*Interest Stacks[^<]*<\/title>/i);
    const entries = parseStackEntries(html);
    const isManga = entries.filter(e => e.kind === "manga").length > entries.filter(e => e.kind === "anime").length;
    if (entries.length === 0) {
      log(`  WARNUNG: Stack ${id} ohne Eintraege - Markup geaendert?`);
      failedStacks++;
      continue;
    }
    // Abgleich gegen den Bestand
    let open = 0;
    const enriched = entries.map(e => {
      const map = e.kind === "anime" ? ownAnime : ownManga;
      const own = map.get(e.id);
      if (!own || ["plan_to_watch", "plan_to_read", "on_hold"].includes(own.status)) open++;
      return { ...e, on_list: !!own, list_status: own?.status ?? null, own_score: own?.score ?? 0 };
    });
    stacks.push({
      id, title: decode(titleM?.[1] ?? `Stack ${id}`).trim(),
      kind: isManga ? "manga" : "anime",
      entries: enriched, open_count: open,
    });
    if ((i + 1) % 20 === 0) log(`  ${i + 1}/${stackIds.length} Stacks...`);
  }

  if (failedStacks > stackIds.length * 0.2) {
    throw new Error(`${failedStacks}/${stackIds.length} Stacks nicht lesbar - vermutlich Markup-Break. Abbruch.`);
  }

  // --- Ergebnis schreiben ---
  const totalEntries = stacks.reduce((s, x) => s + x.entries.length, 0);
  const totalOpen = stacks.reduce((s, x) => s + x.open_count, 0);
  stacks.sort((a, b) => a.open_count - b.open_count || a.title.localeCompare(b.title));

  const out = {
    generated_at: new Date().toISOString(),
    source: "MAL Interest Stacks (public profile pages, HTML)",
    user: MAL_USER,
    stack_count: stacks.length,
    entry_count: totalEntries,
    open_count: totalOpen,
    failed_stacks: failedStacks,
    stacks,
  };
  writeFileSync("interest_stacks.json", JSON.stringify(out, null, 1) + "\n");

  // Slim-Index (ohne Einzeltitel)
  const index = {
    generated_at: out.generated_at,
    user: MAL_USER,
    stack_count: stacks.length,
    open_count: totalOpen,
    stacks: stacks.map(s => ({
      id: s.id, title: s.title, kind: s.kind,
      entries: s.entries.length, open: s.open_count,
    })).sort((a, b) => a.open - b.open),
  };
  writeFileSync("interest_stacks_index.json", JSON.stringify(index, null, 1) + "\n");

  // Schritt-Summary fuer den Action-Run
  summary.push(
    `## Interest-Stacks Harvest`,
    `- Stacks: **${stacks.length}** (davon ${failedStacks} nicht lesbar)`,
    `- Titel gesamt: **${totalEntries}**`,
    `- Noch nicht im Bestand (oder PTW/On-Hold): **${totalOpen}**`,
    ``,
    `Top-Stacks mit den meisten offenen Titeln:`,
    ...index.stacks.slice(0, 10).map(s =>
      `- ${s.open}/${s.entries} offen - [${s.title}](https://myanimelist.net/stacks/${s.id})`),
  );
  if (process.env.GITHUB_STEP_SUMMARY) {
    const { appendFileSync } = await import("node:fs");
    appendFileSync(process.env.GITHUB_STEP_SUMMARY, summary.join("\n") + "\n");
  }
  log("\n" + summary.join("\n"));
}

main().catch(err => { console.error(err); process.exit(1); });
