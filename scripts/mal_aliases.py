#!/usr/bin/env python3
"""
MAL-Alias-Lane v2 (Stand 17.10.2026). PRIMAERQUELLE: anilist_dump.json
(1158 Manga-Einträge mit title.english/native via idMal-Matching) -
kein API-Abruf noetig, laeuft in Sekunden. Jikan nur noch als OPTIONALER
Fallback (via env USE_JIKAN=1) fuer offene Titel ohne EN-Titel im Dump
(GET https://api.jikan.moe/v4/manga/{mal_id}, Rate-Limit 60/min).
Jikan hat KEINE bezahlte Tier (Open Source, spendenfinanziert); wer
mehr braucht, hostet Jikan selbst oder nutzt AniList GraphQL.

Pipeline:
  mal_manga.json (Romaji) --(anilist_dump.json)--> EN-Titel
  open_titles.json (offene Romaji-Titel, Sebastian pflegt)
    -> matcht gegen ALLE Katalog-Snapshots:
       mangaplaza_readinfull.json, jnc_series.json,
       mangaup_catalog.json, mangaplus_catalog.json
  -> abo_snapshots/mal_aliases.json        (Romaji->EN, inkrementell)
  -> abo_snapshots/open_title_hints.json   (Treffer mit Lane/ID/URL)
Diff: abo_changes.json["mal_aliases"] (Zusammenfassung).
Reine stdlib. Laeuft NACH den Katalog-Lanes im Abo-Katalog-Check.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(os.environ.get("REPO_ROOT", str(Path.cwd())))
SNAP_DIR = REPO / "abo_snapshots"
SNAP_DIR.mkdir(exist_ok=True)
SNAP_PATH = SNAP_DIR / "mal_aliases.json"
HINTS_PATH = SNAP_DIR / "open_title_hints.json"
CHANGES_PATH = REPO / "abo_changes.json"
MAL_PATH = REPO / "mal_manga.json"
ANILIST_PATH = REPO / "anilist_dump.json"
OPEN_PATH = REPO / "open_titles.json"
USE_JIKAN = os.environ.get("USE_JIKAN", "0") == "1"
JIKAN = "https://api.jikan.moe/v4/manga/"
REQ_GAP = 1.05
MAX_RETRY = 4
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
CATALOGS = (
    ("mangaplaza_readinfull.json", "MangaPlaza"),
    ("jnc_series.json", "JNC"),
    ("mangaup_catalog.json", "MangaUP"),
    ("mangaplus_catalog.json", "MangaPlus"),
)


def norm(t):
    return " ".join((t or "").lower().split())


def jikan_get(mal_id):
    url = f"{JIKAN}{mal_id}"
    for attempt in range(MAX_RETRY):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < MAX_RETRY - 1:
                time.sleep(5 * (2 ** attempt))
                continue
            if e.code == 404:
                return None
            raise
    return None


def load_aliases():
    """anilist_dump.json -> {romaji: {mal_id, english, japanese}}"""
    out = {}
    if not ANILIST_PATH.exists():
        return out
    with open(ANILIST_PATH, encoding="utf-8") as f:
        dump = json.load(f)
    for e in dump.get("manga", []):
        t = e.get("title") or {}
        rom = t.get("romaji") or ""
        if not rom:
            continue
        rec = {"mal_id": e.get("idMal"), "english": t.get("english"),
               "japanese": t.get("native"), "synonyms": []}
        if rom not in out:          # ersten (besten) Treffer behalten
            out[rom] = rec
    return out


def load_catalogs():
    cats = {}
    for fname, label in CATALOGS:
        p = SNAP_DIR / fname
        if p.exists():
            try:
                with open(p, encoding="utf-8") as f:
                    cats[label] = json.load(f)
            except Exception as e:
                print(f"  Katalog {fname} unlesbar: {e}")
    return cats


def match_catalogs(english, romaji, catalogs):
    cands = {norm(english), norm(romaji)}
    cands.discard("")
    found = {}
    for label, cat in catalogs.items():
        for ctitle, meta in cat.items():
            if norm(ctitle) in cands:
                m = meta if isinstance(meta, dict) else {}
                found[label] = {
                    "catalog_title": ctitle,
                    "title_id": m.get("title_id"),
                    "title_url": m.get("title_url"),
                    "status": m.get("status"),
                }
    return found or None


def main():
    if not MAL_PATH.exists():
        print(f"mal_aliases: {MAL_PATH} fehlt - Lane uebersprungen")
        return 1
    with open(MAL_PATH, encoding="utf-8") as f:
        entries = json.load(f).get("list", [])
    print(f"mal_aliases: {len(entries)} MAL-Eintraege")

    old = {}
    if SNAP_PATH.exists():
        try:
            old = json.loads(SNAP_PATH.read_text(encoding="utf-8"))
        except Exception:
            old = {}

    # AniList-Dump als Primaerquelle (kein API-Call).
    # KOMPLETT uebernehmen (auch Titel ohne mal_manga-Eintrag),
    # damit open_titles-Romaji stets aufgeloest werden koennen.
    aliases = load_aliases()
    new = 0
    for romaji, rec in aliases.items():
        if old.get(romaji) != rec:
            new += 1
        old[romaji] = rec
    # fehlende MAL-Eintraege negativ markieren (Coverage-Info)
    for e in entries:
        romaji = e.get("title")
        if romaji and romaji not in old:
            old[romaji] = {"mal_id": e.get("id"), "english": None,
                           "japanese": None, "synonyms": [],
                           "note": "nicht im AniList-Dump"}
    SNAP_PATH.write_text(
        json.dumps(old, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8")
    print(f"  AniList-Aliase: {len(aliases)} (neu/aktualisiert: {new})")

    # offene Titel aufloesen + matchen
    hits = resolved = jikan_fallbacks = 0
    if OPEN_PATH.exists():
        try:
            with open(OPEN_PATH, encoding="utf-8") as f:
                open_titles = json.load(f)
        except Exception as e:
            print(f"  open_titles.json unlesbar: {e}")
            open_titles = {}
        catalogs = load_catalogs()

        # optionaler Jikan-Fallback: nur offene Titel ohne EN-Titel
        if USE_JIKAN:
            for key, romaji in open_titles.items():
                rec = old.get(romaji) or {}
                if rec.get("english"):
                    continue
                mid = rec.get("mal_id")
                if not mid:
                    continue
                doc = jikan_get(mid)
                d = (doc or {}).get("data") or {}
                eng = d.get("title_english")
                syns = sorted({t["title"] for t in (d.get("titles") or [])
                               if t.get("type") == "Synonym"
                               and t.get("title")})
                if eng or syns:
                    rec.update({"english": eng, "synonyms": syns})
                    jikan_fallbacks += 1
                time.sleep(REQ_GAP)

        hints = {}
        for key, romaji in open_titles.items():
            rec = old.get(romaji)
            if rec:
                hint = {"romaji": romaji, **rec}
            else:
                hint = {"romaji": romaji, "english": None,
                        "japanese": None, "synonyms": [],
                        "note": "nicht in mal_manga.json"}
            hint["catalog_matches"] = match_catalogs(
                hint.get("english"), romaji, catalogs)
            # Synonyme (z.B. Jikan) zusaetzlich matchen
            if not hint["catalog_matches"] and hint.get("synonyms"):
                for syn in hint["synonyms"]:
                    m = match_catalogs(syn, None, catalogs)
                    if m:
                        hint["catalog_matches"] = m
                        break
            if hint.get("english"):
                resolved += 1
            if hint["catalog_matches"]:
                hits += 1
            hints[key] = hint
        HINTS_PATH.write_text(
            json.dumps(hints, ensure_ascii=False, indent=1,
                       sort_keys=True), encoding="utf-8")
        print(f"  offene Titel: {len(hints)}, mit EN-Titel: {resolved}, "
              f"mit Katalog-Treffer: {hits}"
              + (f", Jikan-Fallbacks: {jikan_fallbacks}"
                 if USE_JIKAN else "")
              + " -> open_title_hints.json")
    else:
        print("  open_titles.json fehlt - Hint-Teil uebersprungen")

    changes = {}
    if CHANGES_PATH.exists():
        try:
            changes = json.loads(CHANGES_PATH.read_text(encoding="utf-8"))
        except Exception:
            changes = {}
    changes["mal_aliases"] = {
        "count": len(old),
        "with_english": sum(1 for v in old.values()
                            if v.get("english")),
        "open_titles_matched": hits,
        "jikan_fallback": USE_JIKAN,
    }
    CHANGES_PATH.write_text(
        json.dumps(changes, ensure_ascii=False, indent=1),
        encoding="utf-8")

    print(f"mal_aliases: {len(old)} Titel im Snapshot "
          f"({sum(1 for v in old.values() if v.get('english'))} mit "
          f"EN-Titel)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
