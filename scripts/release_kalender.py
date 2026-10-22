#!/usr/bin/env python3
"""Release-Kalender-Harvester v2 (stdlib only).

Liest den Seed release_series.json und beobachtet je Serie die
Release-Daten von anisearch.de:

  print-de  -> Sprachblock "de" (dt. Bandstand, Status, Verlag, Zeitraum);
               Fallback "en" mit block_lang="en" (EN-Import-Serien, z.B. Viz)
  kobo-en   -> Sprachblock "en" (EN-Bandstand, Status, EN-Verlag)
  kindle-en -> bewusst OHNE Automatik (Amazon unzuverlaessig) -> manual_check

v2-Wechselgrund (22.10.2026, Erst-Lauf rot): Die v1-Quellen sind aus
Datacenter-IPs (GitHub-Runner!) unbrauchbar: kobo.com antwortet 403
(Cloudflare, auch die Startseite), buchhandel.de ist eine JS-SPA ohne
servergerenderte Treffer. anisearch.de ist servergerendert, hat keinen
Bot-Schutz und deckt mit den Sprach-Bloecken BEIDE Lanes (de + en) ab.
Bandstand-Diff (z.B. "11+" -> "12+" oder Status "Laufend" ->
"Abgeschlossen") = Release-/Serien-Ende-Signal; Titel-Harvest und
Bandnummern-Parsing entfallen damit komplett.

Matching: Suchlauf nur beim ERSTEN Mal, dann im State gespeichert
(stabil gegen Ranking-Schwankungen). Suchkette je Serie:
  Override -> Kompletter Titel -> Titel ohne Klammerzusatz -> orig-Titel.
  Von bis zu 3 Kandidaten wird der erste mit passendem Sprachblock und
  passendem Typ (Manga vs. Light Novel, s. Seed-ID/-Titel) gewaehlt -
  LN-Adaptionen stehen auf anisearch oft als Zweit-Treffer neben dem Manga.
  Falsch-Matches fallen im State-Review auf (Series-Titel + Verlag werden
  mitgespeichert).

Ausgabe (wie verlags_announcements: State + Diff, Fehler je Serie toleriert):
  release_kalender.json  - State: je Serie anisearch-Pfad + Release-Block
  release_changes.json   - Diff: je Serie geaenderte Felder seit letztem Lauf
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPO_ROOT = os.environ.get("REPO_ROOT", os.getcwd())
SEED_FILE = os.path.join(REPO_ROOT, "release_series.json")
STATE_FILE = os.path.join(REPO_ROOT, "release_kalender.json")
CHANGES_FILE = os.path.join(REPO_ROOT, "release_changes.json")
BASE = "https://www.anisearch.de"
TIMEOUT = 25
SLEEP_S = 0.4          # sanft gegen die Quelle: ~100-250 Requests je Lauf
MAX_CANDIDATES = 3
LANG_BY_LANE = {"print-de": "de", "kobo-en": "en", "kindle-en": None}
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Explizite Such-Overrides (Serien, die die Standard-Kette nicht findet):
QUERY_OVERRIDES = {
    "2-5d-seduction": "2.5 Dimensional Seduction",
    "2nd-prettiest-girl": "Prettiest Girl in My Class",
}


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def anisearch_search(query):
    """Bis zu MAX_CANDIDATES Manga-Treffer (inkl. LN-Eintraege) fuer eine Query."""
    url = BASE + "/search?type=2&q=" + urllib.parse.quote(query)
    html = fetch(url)
    return re.findall(r'href="(manga/\d+,[a-z0-9-]+)"', html)[:MAX_CANDIDATES]


def parse_lang_blocks(html):
    """Sprach-Bloecke einer anisearch-Serienseite: {'de': {...}, 'en': {...}}"""
    parts = re.split(r'<div class="title" lang="([a-z]{2})"', html)
    out = {}
    for i in range(1, len(parts) - 1, 2):
        lang, seg = parts[i], parts[i + 1]
        block = {}
        for key in ("type", "status", "released", "releases"):
            m = re.search(
                key + r'"><span class="header">[^<]*:</span>\s*([^<]*)', seg)
            if m:
                val = m.group(1).strip().replace("&#8209;", "-")
                if val and val != "?":
                    block[key] = val
        m = re.search(
            r'company"><span class="header">[^<]*:</span>\s*<a[^>]*>([^<]*)</a>',
            seg)
        if m:
            block["company"] = m.group(1).strip()
        if block:
            out[lang] = block
    return out


def strip_parens(title):
    return re.sub(r"\s*\([^)]*\)", "", title).strip()


def type_preference(sid, title):
    """'manga' | 'novel' | None - LN-Adaptionen stehen oft als Zweit-Treffer."""
    low = (sid + " " + title).lower()
    if "light novel" in low or "roman" in low or sid.endswith("-ln"):
        return "novel"
    if "manga" in low:
        return "manga"
    return None


def block_matches_pref(blocks, pref):
    if not pref:
        return True
    for block in blocks.values():
        t = block.get("type", "").lower()
        if pref == "manga" and "manga" in t:
            return True
        if pref == "novel" and "novel" in t:
            return True
    return False


def pick_block(blocks, lane):
    """Passenden Sprachblock je Lane waehlen (print-de mit en-Fallback)."""
    if lane == "print-de":
        if "de" in blocks:
            return "de", blocks["de"]
        if "en" in blocks:
            return "en", blocks["en"]
        return None, None
    if lane == "kobo-en":
        if "en" in blocks:
            return "en", blocks["en"]
        if "de" in blocks and "en" not in blocks:
            return None, None   # EN-Lane verlangt en-Block
        return None, None
    return None, None


def resolve_series(s):
    """Anisearch-Kandidaten suchen und den passenden Eintrag auswaehlen."""
    sid, title, lane = s["id"], s["title"], s.get("lane", "")
    pref = type_preference(sid, title)
    queries = [QUERY_OVERRIDES[sid]] if sid in QUERY_OVERRIDES else [
        title, strip_parens(title), s.get("orig", ""),
    ]
    queries = [q for q in queries if q]

    candidates, fallback = [], None
    for q in queries:
        try:
            candidates = anisearch_search(q)
        except Exception:
            candidates = []
        time.sleep(SLEEP_S)
        if candidates:
            break
    if not candidates:
        return None, "kein anisearch-Treffer fuer Suchkette"

    for path in candidates:
        try:
            blocks = parse_lang_blocks(fetch(BASE + "/" + path))
        except Exception:
            continue
        time.sleep(SLEEP_S)
        lang, block = pick_block(blocks, lane)
        if block and block_matches_pref(blocks, pref):
            return path, None
        if block and fallback is None:
            fallback = (path, lang, block)

    if fallback:
        path, lang, block = fallback
        return ({"path": path, "lang": lang, "note": "Typ-Präferenz nicht erfüllt"}, None)
    return candidates[0], "kein passender Sprachblock"


def harvest_series(s, old_entry):
    """Release-Infos einer Serie ernten. Rueckgabe: entry-dict fuer den State."""
    lane = s.get("lane", "")
    if LANG_BY_LANE.get(lane) is None:  # kindle-en und unbekannte Lanes
        return {"lane": lane, "manual_check": True}

    entry = {"lane": lane}
    if s.get("watch"):
        entry["watch"] = s["watch"]
    if s.get("notes"):
        entry["notes"] = s["notes"]

    # anisearch-Pfad: aus State uebernehmen oder neu aufloesen
    resolved, problem = old_entry.get("anisearch"), None
    if not resolved:
        resolved, problem = resolve_series(s)
        if not resolved:
            entry["error"] = problem or "kein Treffer"
            return entry
    if isinstance(resolved, dict):   # Typ-Fallback-Kandidat
        entry["block_lang"] = resolved["lang"]
        resolved, entry["match_note"] = resolved["path"], resolved["note"]
    entry["anisearch"] = resolved

    try:
        blocks = parse_lang_blocks(fetch(BASE + "/" + resolved))
    except Exception as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"
        return entry
    time.sleep(SLEEP_S)

    lang = entry.get("block_lang") or LANG_BY_LANE[lane]
    if lane == "print-de" and "de" not in blocks and "en" in blocks:
        lang, entry["block_lang"] = "en", "en"   # EN-Import-Serie
    block = blocks.get(lang)
    if not block:
        entry["error"] = f"kein {'de' if lane == 'print-de' else 'en'}-Block (nicht lizenziert?)"
        entry["langs"] = sorted(blocks)
        return entry

    entry["series"] = resolved.split(",", 1)[1] if "," in resolved else resolved
    for key in ("status", "released", "releases", "company"):
        if key in block:
            entry[key] = block[key]
    return entry


def diff_entry(entry, old_entry):
    """Geaenderte Kernfelder je Serie: {'releases': {'old':..,'new':..}, ...}"""
    diffs = {}
    for key in ("status", "released", "releases", "company"):
        old_v, new_v = old_entry.get(key), entry.get(key)
        if old_v is not None and old_v != new_v:
            diffs[key] = {"old": old_v, "new": new_v}
    return diffs


def main():
    now = datetime.now(timezone.utc)
    with open(SEED_FILE, encoding="utf-8") as fh:
        seed = json.load(fh)

    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            old = json.load(fh)
    except (OSError, ValueError):
        old = {"series": {}}
    old_series = old.get("series", {})

    state = {"generated_at": now.isoformat(), "source": "anisearch.de",
             "series": {}}
    changes = {"generated_at": now.isoformat(), "changed": {}}

    n_err = 0
    for s in seed.get("series", []):
        sid = s["id"]
        old_entry = old_series.get(sid, {})
        entry = harvest_series(s, old_entry)
        if "error" in entry:
            n_err += 1
        if old_series:
            d = diff_entry(entry, old_entry)
            if d:
                changes["changed"][sid] = d
        state["series"][sid] = entry

    for path, payload in ((STATE_FILE, state), (CHANGES_FILE, changes)):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)

    n_changed = len(changes["changed"])
    print(f"release_kalender: {len(seed.get('series', []))} Serien im Seed, "
          f"{n_changed} geaendert, {n_err} Fehler/ohne Block")
    if n_err and not any(
            e.get("releases") or e.get("manual_check")
            for e in state["series"].values()):
        print("WARNUNG: Keine Serie mit Release-Daten - Quelle/Parser pruefen")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
