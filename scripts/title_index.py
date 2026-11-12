#!/usr/bin/env python3
"""
title_index.py v1 (Entwurf 04.11.2026) - ZENTRALER TITEL-INDEX.
Fuehrt alle Titel-Quellen des Repos zu EINEM normalisierten Index zusammen:

Quellen (read-only, alle im Repo):
  mal_manga.json / mal_anime.json  -> id, title (Romaji-MAL), status,
                                       score, Lesestand, Datumfelder,
                                       alternative_titles (en/ja/synonyms,
                                       im Export seit 11.11.2026)
  anilist_dump.json                -> idMal-Match: anilist_id, format
                                       (NOVEL/MANGA/TV/...), title.romaji/
                                       english/native, synonyms
  de_titles.tsv (OPTIONAL, Repo-Root; kuratiert) -> dt. Titel je
                                       (medium, mal_id); MAL/AniList pflegen
                                       KEINE dt. Titel, deshalb eigene Lane.
                                       Format (Tabulator-getrennt, Kopfzeile):
                                         mal_id\tmedium\tde_titel\tquelle
                                       quelle = z.B. litfass-bestand,
                                       kobo-ebooks, platform_map, manuell.
                                       Fehlt die Datei -> DE-Lane leer,
                                       KEIN Fehler (graceful).
                                       Bootstrap-Pflicht: dt. Titel einmalig
                                       aus den Knowledge-Bestandsdateien
                                       extrahieren (Titel dt. + Orig.-Spalte;
                                       Orig. via Index selbst aufloesen).

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
  - Synonym-Lane (Patch 12.11.2026, Moebiusschleife Redundanz-Befund): MAL
    alternative_titles (en/ja/synonyms) + AniList synonyms sind jetzt VOLLE
    Varianten-Keys (Beleg: "netojuu no susume" -> 36038 fehlte vorher im
    Index). Jikan/GraphQL-Live-Abrufe bleiben bewusst draussen.

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
DE_TSV_PATH = REPO / "de_titles.tsv"


def load_de_titles():
    """Kuratierte dt. Titel -> {(medium, mal_id): {titel, quelle}}.

    MAL/AniList pflegen KEINE dt. Titel - die DE-Lane lebt in einer
    eigenen, von Hand gepflegten TSV im Repo (Bootstrap aus den
    Knowledge-Bestandsdateien). Fehlt/unlesbar -> leeres Dict, kein Fehler.
    """
    out = {}
    if not DE_TSV_PATH.exists():
        return out
    try:
        with open(DE_TSV_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return out
    for line in lines[1:]:  # Kopfzeile mal_id medium de_titel quelle
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            mal_id = int(parts[0])
        except ValueError:
            continue
        medium = parts[1] if parts[1] in ("manga", "anime") else "manga"
        titel = parts[2].strip()
        quelle = parts[3].strip() if len(parts) > 3 else "manuell"
        if titel:
            out.setdefault((medium, mal_id), []).append(
                {"titel": titel, "quelle": quelle})
    return out


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
    de_titles = load_de_titles()
    records = []
    unmapped = 0
    de_hits = 0

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
            # MAL-Synonym-Lane (Patch 12.11.2026): alternative_titles aus dem
            # Listen-Export als Varianten (fehlt in alten Exports -> leer).
            at = e.get("alternative_titles") or {}
            mal_syns = [x for x in (at.get("synonyms") or [])
                        if isinstance(x, str)]
            al = anilist.get((medium, int(mal_id)))
            if al is None:
                unmapped += 1
            # DE-Lane: kuratierte dt. Titel als zusaetzliche Varianten
            de = de_titles.get((medium, int(mal_id)))
            if de:
                de_hits += len(de)
            # (for-Header erlauben KEINE implizite Fortsetzung per
            # Operator am Zeilenende - deshalb Liste per += aufbauen)
            variants = [e.get("title"), at.get("en"), at.get("ja")]
            variants += mal_syns
            if al:
                variants += [al["romaji"], al["english"], al["native"]]
                variants += al["synonyms"]
            variants = [v for v in variants
                        if v and isinstance(v, str)]
            variants += [d["titel"] for d in (de or [])]
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
                "title_german": [d["titel"] for d in (de or [])],
                "de_sources": [d["quelle"] for d in (de or [])],
                "mal_title_en": at.get("en"),
                "mal_title_ja": at.get("ja"),
                "mal_synonyms": mal_syns,
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
                                "anilist_dump.json"]
                    + (["de_titles.tsv"] if de_titles else []),
                    "count_de_titles": de_hits,
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
          f"({unmapped} ohne AniList-Match, {de_hits} dt. Titel "
          f"aus {DE_TSV_PATH.name if DE_TSV_PATH.exists() else 'keiner DE-Datei'}), "
          f"{n_keys} Index-Keys im TSV")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())