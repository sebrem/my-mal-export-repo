#!/usr/bin/env node
// Fetch der RSS-Feeds von anime2you.de, manga2you.de und ADN News (DE).
// Bewusst ohne Dependencies (kein rss-parser): einfacher Item-Regex reicht
// fuer Titel/Link/Datum. Blockiert ein Feed (403/Challenge/etc.), wird das
// in anime2you_feed_status.json diagnostiziert, statt den Lauf crashen zu lassen.
// ADN (28.10.2026): news.animationdigitalnetwork.com blockt den Mistral-Proxy,
// GitHub-Runner koennen aber durchkommen (analog Anime2You-Block-Beobachtung).
// Workflow-Name/Dateinamen bewusst unveraendert gelassen (Wächter-Kompatibilitaet,
// Newsletter-Check liest weiterhin anime2you_news.json; ADN-Items tragen site "adn-news").

import { readFileSync, writeFileSync, existsSync } from "node:fs";

const FEEDS = [
  { site: "anime2you", url: "https://www.anime2you.de/feed/" },
  { site: "manga2you", url: "https://www.manga2you.de/feed/" },
  { site: "adn-news", url: "https://news.animationdigitalnetwork.com/de/feed/" },
];
const NEWS_FILE = "anime2you_news.json";
const STATUS_FILE = "anime2you_feed_status.json";
const MAX_ITEMS_PER_SITE = 50;
const UA = "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0";

function parseItems(xml) {
  const items = [];
  const blocks = xml.match(/<item[\s\S]*?<\/item>/gi) || [];
  for (const b of blocks) {
    const pick = (tag) => {
      const m = b.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`, "i"));
      if (!m) return null;
      let v = m[1].trim();
      v = v.replace(/<!\[CDATA\[|\]\]>/g, "");
      v = v.replace(/<[^>]+>/g, ""); // evtl. eingebettetes HTML im Titel
      return decodeEntities(v).trim();
    };
    const item = {
      title: pick("title"),
      link: pick("link"),
      pubDate: pick("pubDate") || pick("date"),
      guid: pick("guid"),
    };
    if (item.title && item.link) items.push(item);
  }
  return items;
}

function decodeEntities(s) {
  const map = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };
  return s.replace(/&#(\d+);/g, (_, n) => String.fromCharCode(+n))
          .replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCharCode(parseInt(n, 16)))
          .replace(/&(\w+);/g, (m, e) => map[e] || m);
}

function loadPrevious() {
  try {
    if (existsSync(NEWS_FILE)) return JSON.parse(readFileSync(NEWS_FILE, "utf8"));
  } catch (e) { /* neu anfangen */ }
  return { updated: null, items: [] };
}

async function fetchFeed(url) {
  const res = await fetch(url, {
    headers: {
      "User-Agent": UA,
      "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
      "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
    },
    redirect: "follow",
  });
  const body = await res.text();
  return { status: res.status, ok: res.ok, contentType: res.headers.get("content-type") || "", body };
}

const prev = loadPrevious();
const status = { updated: new Date().toISOString(), feeds: {} };
const allItems = [];

for (const feed of FEEDS) {
  const key = feed.site;
  try {
    const r = await fetchFeed(feed.url);
    const looksXml = /xml|rss/i.test(r.contentType) || r.body.trimStart().startsWith("<?xml");
    if (r.ok && looksXml) {
      const items = parseItems(r.body).slice(0, MAX_ITEMS_PER_SITE);
      status.feeds[key] = { ok: true, http: r.status, itemsFound: items.length, blocked: false };
      for (const it of items) allItems.push({ site: key, ...it });
      console.log(`${key}: OK (${r.status}), ${items.length} Items.`);
    } else {
      const challenge = /cloudflare|challenge|cf-ray|just a moment|enable javascript/i.test(r.body);
      status.feeds[key] = {
        ok: false, blocked: true, http: r.status,
        contentType: r.contentType,
        reason: challenge ? "cloudflare-challenge" : `not-xml-or-http-${r.status}`,
        bodySnippet: r.body.replace(/\s+/g, " ").slice(0, 200),
      };
      console.log(`${key}: BLOCKIERT (HTTP ${r.status}, ${status.feeds[key].reason}).`);
    }
  } catch (err) {
    status.feeds[key] = { ok: false, blocked: true, reason: "fetch-error", error: String(err).slice(0, 300) };
    console.log(`${key}: FEHLER ${err}`);
  }
}

// Dedup gegen den vorherigen Stand (per Link) und neueste zuerst behalten
const seen = new Set(prev.items.map((i) => i.link));
const merged = [...allItems.filter((i) => !seen.has(i.link)), ...prev.items]
  .filter((i, idx, arr) => arr.findIndex((x) => x.link === i.link) === idx)
  .slice(0, MAX_ITEMS_PER_SITE * FEEDS.length);

const anyBlocked = Object.values(status.feeds).some((f) => f.blocked);
status.anyBlocked = anyBlocked;

writeFileSync(STATUS_FILE, JSON.stringify(status, null, 2));
writeFileSync(NEWS_FILE, JSON.stringify({ updated: status.updated, items: merged }, null, 2));

console.log(`Status: ${JSON.stringify(status.feeds)}`);
if (anyBlocked) {
  console.log("HINWEIS: Mindestens ein Feed blockt auch aus der GitHub-Infrastruktur - Diagnose in anime2you_feed_status.json.");
}
