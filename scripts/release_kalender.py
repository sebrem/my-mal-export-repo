#!/usr/bin/env python3
"""Release-Kalender-Harvester (stdlib only).

Liest den Seed release_series.json (Repo-Root) und beobachtet je Serie die
Listung neuer Titel (Bd.-Nummern, Omnibus-Ausgaben, Vorbestellungen):

  kobo-en    -> kobo US-Suche (Server-HTML, wie im Skill dokumentiert)
  print-de   -> buchhandel.de/VLB-Suche (generisch, alle dt. Verlage)
  kindle-en  -> bewusst OHNE Automatik (Amazon unzuverlaessig) -> manual_check

Ausgabe (wie verlags_announcements: State + Diff, Fehler je Serie toleriert):
  release_kalender.json  - State: je Serie die aktuell gelisteten Titel
  release_changes.json   - Diff: new/removed Titel je Serie seit letztem Lauf

Design-Entscheidung v1: KEIN Bandnummern-Parsing. Stattdessen werden je Serie
die gelisteten Produkt-Titel geerntet und per Diff erkannt - ein NEUER Titel in
der Trefferliste = neuer gelisteter Release/Vorbestellung. Das ist robust gegen
Omnibus-/LN-/Manga-Lane-Fallen: Der Diff meldet nur "neu gelistet", die Lane-
Regeln (z.B. "Omnibus-Lane, Einzelbaende NICHT kaufen") bleiben Konsum-Regeln
und werden beim Lesen des Diffs angewendet - der Harvest wertet nichts.

Such-Override: Manche Serien brauchen Sonder-Queries (Autor-Suche statt
Titelsuche, s. Notes im Seed). Bis das Feld ins Seed-Format einzieht, steht
der Override zentral hier in QUERY_OVERRIDES.
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPO_ROOT = os.environ.get("REPO_ROOT", os.getcwd())
SEED_FILE = os.path.join(REPO_ROOT, "release_series.json")
STATE_FILE = os.path.join(REPO_ROOT, "release_kalender.json")
CHANGES_FILE = os.path.join(REPO_ROOT, "release_changes.json")
TIMEOUT = 25
MAX_TITLES_PER_SERIES = 12   # Trefferliste deckeln: Series-Seite, nicht Katalog
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) HeadlessChrome/126 Safari/537.36")

# Sonder-Suchen (v1-Override, spater optional als Feld "search_query" im Seed):
#   anjou-san: kobo-Titelsuche versagt - Autor-Suche + Release-Sortierung
#   water-magician-manga: kobo-Titelsuche zeigt primär LN-Lane - Mangaka-Suche
QUERY_OVERRIDES = {
    "anjou-san": "Yuichi Katou",
    "water-magician-manga": "Inori",  # Mangaka der Water Magician Manga-Adaption
}

# buchhandel.de listet auch Hörbücher/Foreign-Language-Ausgaben; Grundfilter
NOISE_RE = re.compile(r"(hörbuch|hoerbuch|audiobook|kalender|sticker|poster)", re.I)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def extract_titles(html, base_url, link_filter):
    """Generische Anchor-Extraktion: (titel, url) mit Filter je Quelle."""
    out, seen = [], set()
    for href, inner in re.findall(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S
    ):
        title = clean(inner)
        if not (10 <= len(title) <= 250):
            continue
        if not link_filter(href, title):
            continue
        if href.startswith("/"):
            m = re.match(r"(https?://[^/]+)", base_url)
            href = m.group(1) + href
        key = (title, href)
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": title, "url": href})
    return out


def search_kobo_en(title, series_id):
    query = QUERY_OVERRIDES.get(series_id, title)
    url = ("https://www.kobo.com/us/en/search?query="
           + urllib.parse.quote(query) + "&sort=publication&fcmedia=Ebook")
    html = fetch(url)
    # kobo-Produktlinks: /us/en/audiobook/... oder /us/en/ebook/...
    items = extract_titles(html, url, lambda h, t: "/ebook/" in h)
    if not items:  # Fallback ohne Medium-Filter (Omnibus-Ausgaben etc.)
        items = extract_titles(html, url, lambda h, t: "/us/en/" in h)
    return dedup_titles(items)


def search_print_de(title, series_id):
    query = QUERY_OVERRIDES.get(series_id, title)
    url = "https://www.buchhandel.de/search?q=" + urllib.parse.quote(query)
    html = fetch(url)
    # buchhandel.de-Produktlinks: /artikel/...
    items = extract_titles(html, url, lambda h, t: "/artikel/" in h)
    items = [i for i in items if not NOISE_RE.search(i["title"])]
    return dedup_titles(items)


def dedup_titles(items):
    seen, out = set(), []
    for it in items:
        if it["title"].lower() in seen:
            continue
        seen.add(it["title"].lower())
        out.append(it)
        if len(out) >= MAX_TITLES_PER_SERIES:
            break
    return out


def main():
    now = datetime.now(timezone.utc)
    with open(SEED_FILE, encoding="utf-8") as fh:
        seed = json.load(fh)

    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            old = json.load(fh)
    except (OSError, ValueError):
        old = {"series": {}}

    state = {"generated_at": now.isoformat(), "series": {}}
    changes = {"generated_at": now.isoformat(), "new": {}, "removed": {}}

    n_err = 0
    for s in seed.get("series", []):
        sid, lane, title = s["id"], s["lane"], s["title"]
        entry = {"lane": lane, "query": QUERY_OVERRIDES.get(sid, title)}
        if s.get("watch"):
            entry["watch"] = s["watch"]
        if s.get("notes"):
            entry["notes"] = s["notes"]

        if lane == "kobo-en":
            try:
                entry["titles"] = search_kobo_en(title, sid)
            except Exception as exc:
                entry["titles"] = []
                entry["error"] = f"{type(exc).__name__}: {exc}"
                n_err += 1
        elif lane == "print-de":
            try:
                entry["titles"] = search_print_de(title, sid)
            except Exception as exc:
                entry["titles"] = []
                entry["error"] = f"{type(exc).__name__}: {exc}"
                n_err += 1
        else:  # kindle-en und unbekannte Lanes: bewusst manuell
            entry["titles"] = []
            entry["manual_check"] = True

        state["series"][sid] = entry

        prev = {
            it["title"].lower()
            for it in old.get("series", {}).get(sid, {}).get("titles", [])
        }
        cur = {it["title"].lower() for it in entry.get("titles", [])}
        new_items = [it for it in entry.get("titles", []) if it["title"].lower() not in prev]
        removed = sorted(prev - cur)
        if new_items:
            changes["new"][sid] = new_items
        if removed:
            changes["removed"][sid] = removed

    for path, payload in ((STATE_FILE, state), (CHANGES_FILE, changes)):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)

    n_new = sum(len(v) for v in changes["new"].values())
    print(f"release_kalender: {len(seed.get('series', []))} Serien im Seed, "
          f"{n_new} neue Titel, {n_err} Fehler")
    if n_err and all(not s.get("titles") for s in state["series"].values()):
        print("WARNUNG: Alle Suchen fehlgeschlagen - URLs/Parser pruefen")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
