#!/usr/bin/env python3
"""
title_index.py v1 (Entwurf 04.11.2026) - ZENTRALER TITEL-INDEX.
Fuehrt alle Titel-Quellen des Repos zu EINEM normalisierten Index zusammen:

Quellen (read-only, alle im Repo):
  mal_manga.json / mal_anime.json  -> id, title (Romaji-MAL), status,
                                       score, Lesestand, Datumfelder
  anilist_dump.json                -> idMal-Match: anilist_id, format
                                       (NOVEL/MANGA/TV/...), title.romaji/
                                       english/native, synonyms

Ausgaben (ins Repo committet):
  abo_snapshots/title_index.json   -> Vollindex: ein Record je
                                       (medium, mal_id) mit ALLEN Varianten
  abo_snapshots/title_index.tsv    -> grep-bar: EINE ZEILE JE NORMALISIERTEM
                                       KEY (Index im Wortsinn :-P):
                                       key \t mal_id \t medium \t format \t
                                       canonical_title
                                       -> Ad-hoc-Verifikation per grep/-F im
                                          Montags-Abgleich, Agent-Sessions
                                          und geplanten Tasks, OHNE JSON-Parsing
                                          und ohne drei Dateien zu kreuzen.

Normalisierung = dieselbe Kette wie title_lookup.py (NFKC -> Macron-Fold ->
lower -> Satzzeichen raus), damit Keys und Query dasselbe Vokabular haben.

Design-Regeln:
  - Primaerschluessel: (medium, mal_id) - MAL-Titel sind NUR Varianten, nie
    Schluessel (Beleg: ここカノ-Duo MAL 194548/194549 mit identischem Titel).
  - LN-vs-Manga-Differenzierung: AniList-Feld `format` (NOVEL vs MANGA);
    der MAL-Listen-Export hat dafuer KEIN Feld. Eintraege OHNE AniList-Match
    bekommen format=None + note "unmapped" (AniList-Pipeline nachfassen).
  - KEIN API-Abruf: nur die drei lokalen Dateien, laeuft in Sekunden.
  - Jikan/AniList-GraphQL-Erweiterung (Synonyme, DE-Titel) bewusst als
    optionale Ausbaustufe, nicht Teil v1.

Integration: eigener Workflow-Schritt nach anilist_export im Montags-Job
(s. Workflow-Datei im Begleit-Canvas), Commit wie die anderen Snapshots.
Doku: knowledge/anime-manga/titel-matching.md (fuehrende Instanz).
"""
import json
import os
import unicodedata
from pathlib import Path

REPO = Path(os.environ.get("REPO_ROOT", str(Path(__file__).resolve()
                                             .parent.parent)))
SNAP_DIR = REPO / "abo_snapshots"
SNAP_DIR.mkdir(exist_ok=True)
INDEX_PATH = SNAP_DIR / "title_index.json"
TSV_PATH = SNAP_DIR / "title_index.tsv"
MAL_FILES = (("mal_manga.json", "manga"), ("mal_anime.json", "anime"))
ANILIST_PATH = REPO / "anilist_dump.json"


def norm(t):
    """NFKC -> Macron/Umlaut-Fold -> lower -> Satzzeichen raus (wie lookup)."""
    if not t:
        return ""
    s = unicodedata.normalize("NFKC", str(t))
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = "".join(c if c.isalnum() or c == " " else " " for c in s)
    return " ".join(s.split())


def load_anilist_by_mal():
    """anilist_dump -> {(medium, idMal): record} (erster Treffer gewinnt)."""
    out = {}
    if not ANILIST_PATH.exists():
        print(f"  WARNUNG: {ANILIST_PATH.name} fehlt - nur MAL-Daten")
        return out
    with open(ANILIST_PATH, encoding="utf-8") as f:
        dump = json.load(f)
    for medium in ("manga", "anime"):
        for e in dump.get(medium, []):
            if not isinstance(e, dict):
                continue
            mid = e.get("idMal")
            if not mid:
                continue
            key = (medium, int(mid))
            if key not in out:
                t = e.get("title") or {}
                out[key] = {
                    "anilist_id": e.get("id"),
                    "format": e.get("format"),
                    "romaji": t.get("romaji"),
                    "english": t.get("english"),
                    "native": t.get("native"),
                    "synonyms": [s for s in (e.get("synonyms") or [])
                                 if isinstance(s, str)],
                }
    return out


def main():
    anilist = load_anilist_by_mal()
    records = []
    unmapped = 0

    for fname, medium in MAL_FILES:
        path = REPO / fname
        if not path.exists():
            print(f"  {fname} fehlt - uebersprungen")
            continue
        with open(path, encoding="utf-8") as f:
            entries = json.load(f).get("list", [])
        for e in entries:
            mal_id = e.get("id")
            if not mal_id:
                continue
            ls = e.get("list_status") or {}
            al = anilist.get((medium, int(mal_id)))
            if al is None:
                unmapped += 1
            # Varianten-Menge: MAL-Titel + AniList romaji/english/native/
            # synonyms; dann normalisieren, Dubletten und Leerstrings raus.
            variants = []
            for v in [e.get("title"),
                      al and al["romaji"], al and al["english"],
                      al and al["native"]] + (al["synonyms"] if al else []):
                if v and isinstance(v, str):
                    variants.append(v)
            keys = sorted({norm(v) for v in variants} - {""})
            rec = {
                "mal_id": mal_id,
                "medium": medium,
                "anilist_id": al["anilist_id"] if al else None,
                "format": al["format"] if al else None,
                "status": ls.get("status"),
                "score": ls.get("score"),
                "volumes_read": ls.get("num_volumes_read"),
                "chapters_read": ls.get("num_chapters_read"),
                "start_date": ls.get("start_date"),
                "finish_date": ls.get("finish_date"),
                "title_mal": e.get("title"),
                "title_romaji": al["romaji"] if al else None,
                "title_english": al["english"] if al else None,
                "title_native": al["native"] if al else None,
                "synonyms": al["synonyms"] if al else [],
                "note": None if al else "unmapped (kein AniList-Treffer)",
                "keys": keys,
            }
            records.append(rec)

    records.sort(key=lambda r: (r["medium"], r["mal_id"]))
    INDEX_PATH.write_text(
        json.dumps({"generated_at_utc": __import__("time").strftime(
                        "%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
                    "count_records": len(records),
                    "count_unmapped": unmapped,
                    "sources": ["mal_manga.json", "mal_anime.json",
                                "anilist_dump.json"],
                    "records": records},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    # grep-barer TSV: eine Zeile JE KEY (nicht je Titel) - das ist der Index
    with open(TSV_PATH, "w", encoding="utf-8") as f:
        f.write("key\tmal_id\tmedium\tformat\tcanonical_title\n")
        for r in records:
            canonical = (r["title_native"] or r["title_romaji"]
                         or r["title_mal"] or "")
            for k in r["keys"]:
                f.write(f"{k}\t{r['mal_id']}\t{r['medium']}\t"
                        f"{r['format'] or '-'}\t{canonical}\n")

    n_keys = sum(len(r["keys"]) for r in records)
    print(f"title_index: {len(records)} Titel-Records "
          f"({unmapped} ohne AniList-Match), {n_keys} Index-Keys im TSV")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())