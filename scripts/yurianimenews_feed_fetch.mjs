#!/usr/bin/env node
// Fetch der Squarespace-RSS-Feeds von yurianimenews.com (Sektionen per ?format=rss).
// Der Feed ist auf der Seite NICHT verlinkt (kein <link rel="alternate">);
// auffindbar ueber die Kategorie-URLs mit Query-Suffix (04.11.2026).
// Retention je Feed ~20 Items -> taeglicher Harvest schliesst Rotationsluecken
// (analog Crunchyroll-Pattern vom 02.11.2026).
// Besonderheit: Die <description> enthaelt den VOLLTEXT inkl. Release-Daten,
// Synopsen und Shop-/Affiliate-Links (amzn.to, link.amazon, bookwalker.com ...).
// Daher wird je Item der ent-HTML-te Volltext mitgespeichert (gekappen auf
// MAX_TEXT_CHARS) plus extrahierte Shop-Links; das System muss dafuer
// NICHT auf die Artikel-Seite gehen.
// Ausgabe: yurianimenews_news.json (Items) + yurianimenews_feed_status.json.
// Workflow: Schritt im bestehenden anime2you-feed.yml (taeglich 06:30 UTC),
// Wächter-EXPECTED unveraendert (Workflow-Dateiname bleibt anime2you-feed.yml).

import { readFileSync, writeFileSync, existsSync } from "node:fs";

const FEEDS = [
  { section: "news", url: "https://www.yurianimenews.com/news?format=rss" },
  { section: "yurimanga", url: "https://www.yurianimenews.com/yurimanga?format=rss" },
  { section: "yurianime", url: "https://www.yurianimenews.com/yurianime?format=rss" },
  { section: "yuri-vn", url: "https://www.yurianimenews.com/yuri-vn?format=rss" },
  { section: "reviews", url: "https://www.yurianimenews.com/reviews?format=rss" },
];
const NEWS_FILE = "yurianimenews_news.json";
const STATUS_FILE = "yurianimenews_feed_status.json";
const MAX_ITEMS_PER_SITE = 50;
const MAX_ITEMS_TOTAL = 250;
const MAX_TEXT_CHARS = 6000;
const MAX_SHOP_LINKS = 30;
const UA = "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0";

function decodeEntities(s) {
  const map = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", hellip: "…", rsquo: "’", lsquo: "‘", rdquo: "”", ldquo: "“", mdash: "—", ndash: "–" };
  return s.replace(/&#(\d+);/g, (_, n) => String.fromCharCode(+n))
          .replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCharCode(parseInt(n, 16)))
          .replace(/&(\w+);/g, (m, e) => map[e] || m);
}

function stripHtml(html) {
  return decodeEntities(html)
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(p|div|h\d|li|tr)>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/[ \t]+/g, " ")
    .replace(/\n\s*\n\s*\n+/g, "\n\n")
    .replace(/\s+$/g, "")
    .trim();
}

function extractShopLinks(rawHtml) {
  const links = [];
  const seen = new Set();
  const re = /href="(https?:\/\/[^"]+)"/gi;
  let m;
  while ((m = re.exec(rawHtml)) !== null && links.length < MAX_SHOP_LINKS) {
    const url = decodeEntities(m[1]);
    if (/yurianimenews\.com/i.test(url)) continue;
    if (seen.has(url)) continue;
    seen.add(url);
    links.push(url);
  }
  return links;
}

function extractReleaseDates(text) {
  const re = /\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4}\b/g;
  return [...new Set((text.match(re) || []).map((d) => d.replace(/\s+/g, " ")))].slice(0, 40);
}

function parseItems(xml) {
  const items = [];
  const blocks = xml.match(/<item[\s\S]*?<\/item>/gi) || [];
  for (const b of blocks) {
    const pick = (tag) => {
      const m = b.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`, "i"));
      if (!m) return null;
      let v = m[1].trim();
      v = v.replace(/<!\[CDATA\[|\]\]>/g, "");
      v = v.replace(/<[^>]+>/g, "");
      return decodeEntities(v).trim();
    };
    const descRaw = (() => {
      const m = b.match(new RegExp(`<description[^>]*>([\\s\\S]*?)</description>`, "i"));
      if (!m) return "";
      return m[1].replace(/<!\[CDATA\[|\]\]>/g, "").trim();
    })();
    const text = stripHtml(descRaw);
    const item = {
      title: pick("title"),
      link: pick("link"),
      pubDate: pick("pubDate") || pick("date"),
      guid: pick("guid"),
      text: text.slice(0, MAX_TEXT_CHARS),
      textTruncated: text.length > MAX_TEXT_CHARS,
      releaseDates: extractReleaseDates(text),
      shopLinks: extractShopLinks(descRaw),
    };
    if (item.title && item.link) items.push(item);
  }
  return items;
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
      "Accept": "application/rss+xml, application/xml, text/xml, */*",
      "Accept-Language": "en-US,en;q=0.9",
    },
    redirect: "follow",
  });
  const body = await res.text();
  return { status: res.status, ok: res.ok, contentType: res.headers.get("content-type") || "", body };
}

const prev = loadPrevious();
const status = { updated: new Date().toISOString(), source: "yurianimenews.com (Squarespace, ?format=rss)", feeds: {} };
const allItems = [];

for (const feed of FEEDS) {
  const key = feed.section;
  try {
    const r = await fetchFeed(feed.url);
    const looksXml = /xml|rss/i.test(r.contentType) || r.body.trimStart().startsWith("<?xml");
    if (r.ok && looksXml) {
      const items = parseItems(r.body).slice(0, MAX_ITEMS_PER_SITE);
      status.feeds[key] = { ok: true, http: r.status, itemsFound: items.length, blocked: false };
      for (const it of items) allItems.push({ site: "yurianimenews-" + key, ...it });
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
  .slice(0, MAX_ITEMS_TOTAL);

const anyBlocked = Object.values(status.feeds).some((f) => f.blocked);
status.anyBlocked = anyBlocked;

writeFileSync(STATUS_FILE, JSON.stringify(status, null, 2));
writeFileSync(NEWS_FILE, JSON.stringify({ updated: status.updated, items: merged }, null, 2));

console.log(`Status: ${JSON.stringify(status.feeds)}`);
console.log(`Gesamt: ${merged.length} Items in ${NEWS_FILE}.`);
if (anyBlocked) {
  console.log("HINWEIS: Mindestens ein Feed blockt - Diagnose in yurianimenews_feed_status.json.");
}
