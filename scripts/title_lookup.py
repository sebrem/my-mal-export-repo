#!/usr/bin/env python3
"""
title_lookup.py v1 (Entwurf 04.11.2026) - Titel-Matching-Werkzeugkasten als
ausfuehrbares Werkzeug. Aufloesung BELIEBIGER Schreibweise (Kanji, Romaji
mit/ohne Macron, EN, Synonym) zu MAL-ID, Medium und allen bekannten Aliasen.

Quellen (alle bereits im Repo, kein API-Abruf noetig):
  anilist_dump.json  -> title.romaji / title.english / title.native
                        + synonyms, mit idMal
  mal_manga.json     -> Titel + id (zusatzliche Abdeckung ausserhalb AniList)
  mal_anime.json     -> Titel + id (Anime-Lane, --medium anime)

Normalisierungs-Kette (faengt die bekannten Fallen ab):
  1. NFKC                 -> Vollbreite-Ziffern/Zeichen (Amazon "Vol.１０")
  2. NFKD + Akzent-Fold   -> Macron/Umlaut-Varianten (Kokou/Kokō, û/uu)
  3. lower + strip        -> Gross/Kleinschreibung, Leerzeichen
  4. Satzzeichen raus      -> Bindestrich-Fallen (Bijutsu-bu/Bijutsubu)
  5. optional "squash"    -> ALLE Nicht-Alphanumerika raus (2. Fuzzy-Stufe)

Aufruf-Beispiele:
  python3 scripts/title_lookup.py "孤高なカノジョと、彼女の部屋でシてること"
  python3 scripts/title_lookup.py "Kokou na Kanojo"            # Teilstring
  python3 scripts/title_lookup.py "Koko wa Ima kara Rinri"    # Teilstring
  python3 scripts/title_lookup.py --medium anime "Kono Bijutsu"
  python3 scripts/title_lookup.py --squash "bijutsubu"         # 2. Stufe

Exit-Codes: 0 = Treffer, 1 = kein Treffer (fuer Skript-Verkettung).
Teilstring-Match ab 4 normalisierten Zeichen (kein Match gegen "no"/"wa").

Integration: als On-Demand-Step im Abo-Katalog-Check-Workflow
(workflow_dispatch, continue-on-error) ODER simpel als manuelles Werkzeug
fuer Agent-Session und Montags-Abgleich. KEIN git commit noetig (read-only).
Dokumentation: knowledge/anime-manga/titel-matching.md (fuehrende Instanz).
"""
import json
import os
import sys
import unicodedata
from pathlib import Path

REPO = Path(os.environ.get("REPO_ROOT", str(Path(__file__).resolve()
                                             .parent.parent)))
ANILIST_PATH = REPO / "anilist_dump.json"
MAL_MANGA_PATH = REPO / "mal_manga.json"
MAL_ANIME_PATH = REPO / "mal_anime.json"
MIN_LEN = 4          # Mindestlaenge fuer Teilstring-Match
SQUASH_MIN_LEN = 5  # Mindestlaenge fuer Fuzzy-Stufe 2 (alles gestrippt)


def norm(t):
    """Normalisierungs-Kette: NFKC -> NFKD-Fold -> lower -> Satzzeichen raus."""
    if not t:
        return ""
    s = unicodedata.normalize("NFKC", str(t))
    s = unicodedata.normalize("NFKD", s)
    # Kombinierende Zeichen (Macrons, Umlaute) entfernen: ō->o, û->u, é->e
    s = "".join(c for c in s
                if not unicodedata.combining(c))
    s = s.lower()
    # Satzzeichen/Trennzeichen raus, Leerzeichen bleiben (Stufe 1)
    s = "".join(c if c.isalnum() or c == " " else " "
                for c in s)
    return " ".join(s.split())


def squash(t):
    """Fuzzy-Stufe 2: ALLE Nicht-Alphanumerika raus (Bijutsu-bu=Bijutsubu)."""
    return "".join(c for c in norm(t) if c.isalnum())


def load_index(medium_filter=None):
    """Vereinheitlichter Index: norm_variante -> [{mal_id, medium, ...}]"""
    index = {}  # key -> list of records

    def add(key, rec):
        if not key or len(key) < 2:
            return
        index.setdefault(key, [])
        # Dubletten vermeiden (gleiche mal_id + medium + quelle)
        for existing in index[key]:
            if (existing.get("mal_id") == rec.get("mal_id")
                    and existing.get("medium") == rec.get("medium")
                    and existing.get("quelle") == rec.get("quelle")):
                return
        index[key].append(rec)

    # 1) AniList-Dump: romaji / english / native / synonyms
    #    NUR die bekannten Media-Keys lesen ("manga"/"anime"), alle anderen
    #    Top-Level-Keys (z.B. "unmapped", "meta") enthalten Strings/Listen
    #    ohne Titel-Objekte - Beleg-Fehler 04.11.2026 (AttributeError).
    if ANILIST_PATH.exists():
        with open(ANILIST_PATH, encoding="utf-8") as f:
            dump = json.load(f)
        for medium in [m for m in ("manga", "anime")
                       if not medium_filter or m == medium_filter]:
            for e in dump.get(medium, []):
                if not isinstance(e, dict):
                    continue    # defensive: Strings/None ueberspringen
                t = e.get("title") or {}
                if not isinstance(t, dict):
                    continue
                base = {"mal_id": e.get("idMal"), "medium": medium,
                        "quelle": "anilist", "romaji": t.get("romaji"),
                        "english": t.get("english"),
                        "japanese": t.get("native")}
                for variant in [t.get("romaji"), t.get("english"),
                                t.get("native")] + (e.get("synonyms") or []):
                    add(norm(variant), dict(base))
    # 2) MAL-Exporte: Titel direkt (deckt Titel ohne AniList-Mapping ab)
    for path, medium in ((MAL_MANGA_PATH, "manga"), (MAL_ANIME_PATH, "anime")):
        if medium_filter and medium != medium_filter:
            continue
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for e in json.load(f).get("list", []):
                add(norm(e.get("title")),
                    {"mal_id": e.get("id"), "medium": medium,
                     "quelle": "mal_export",
                     "romaji": e.get("title"),
                     "status": e.get("status"), "score": e.get("score")})
    return index


def merge_records(records):
    """Gleiche mal_id+medium ueber Quellen hinweg verschmelzen."""
    by_key = {}
    for r in records:
        k = (r.get("mal_id"), r.get("medium"))
        if k not in by_key:
            by_key[k] = {"mal_id": r.get("mal_id"), "medium": r.get("medium"),
                         "quellen": set(), "romaji": None, "english": None,
                         "japanese": None, "status": None, "score": None}
        m = by_key[k]
        m["quellen"].add(r.get("quelle"))
        for field in ("romaji", "english", "japanese", "status", "score"):
            if r.get(field) is not None and m.get(field) is None:
                m[field] = r.get(field)
    return list(by_key.values())


def lookup(query, medium_filter=None, use_squash=False):
    nq = norm(query)
    if len(nq) < MIN_LEN:
        return [], "Query zu kurz (min %d normalisierte Zeichen)" % MIN_LEN
    index = load_index(medium_filter)

    # Stufe 1: exakter normalisierter Treffer, danach Teilstring
    exact = index.get(nq)
    if exact:
        # Stufe 1b - Alias-Erweiterung: fuer jeden Treffer zusaetzlich
        # dessen Romaji/EN-Titel in den Index nachschlagen. So kommen
        # Geschwister-Eintraege mit GLEICHEM Titel aber ANDEREM Key mit
        # (Beleg 04.11.2026: Kanji-Treffer MAL 194548 zog den Romaji-Key
        # nach und holte das LN-Gegenstueck MAL 194549 aus mal_manga.json).
        merged_keys = set()
        queue = list(exact)
        hits = []
        while queue:
            r = queue.pop(0)
            hits.append(r)
            for alias in (r.get("romaji"), r.get("english"),
                          r.get("japanese")):
                if not alias:
                    continue
                for key in (norm(alias), alias):
                    if key in merged_keys or key not in index:
                        continue
                    merged_keys.add(key)
                    for sib in index[key]:
                        if not any(x is sib for x in hits):
                            queue.append(sib)
        return merge_records(hits), "exakt (normalisiert, mit Alias-Erweiterung)"
    hits = []
    for key, recs in index.items():
        if nq in key:
            hits.extend(recs)
    if hits:
        return merge_records(hits), "Teilstring (normalisiert)"

    # Stufe 2: squash-Fuzzy (nur bei ausreichender Laenge)
    if use_squash:
        sq = squash(query)
        if len(sq) >= SQUASH_MIN_LEN:
            hits = []
            for key, recs in index.items():
                if sq in squash(key) or sq == squash(key):
                    hits.extend(recs)
            if hits:
                return merge_records(hits), "squash-Fuzzy"
    return [], "kein Treffer (Stufen 1-2)"


def main():
    args = [a for a in sys.argv[1:]]
    use_squash = "--squash" in args
    args = [a for a in args if a != "--squash"]
    medium_filter = None
    if "--medium" in args:
        i = args.index("--medium")
        medium_filter = args[i + 1]
        args = args[i + 2:]
    if not args:
        print(__doc__)
        return 2
    query = " ".join(args)

    records, mode = lookup(query, medium_filter, use_squash)
    print(f"Query: {query}")
    print(f"Match-Modus: {mode}")
    if not records:
        print("KEIN TREFFER - Negativ-Befund nur gueltig, wenn auch "
              "andere Schreibweisen (s. titel-matching.md) geprueft wurden.")
        return 1
    records.sort(key=lambda r: (r.get("mal_id") or 0))
    for r in records:
        print(f"\nMAL-ID {r['mal_id']}  [{r['medium']}]  "
              f"(Quellen: {', '.join(sorted(r['quellen']))})")
        print(f"  Romaji : {r.get('romaji')}")
        print(f"  EN     : {r.get('english')}")
        print(f"  JP     : {r.get('japanese')}")
        if r.get("status") or r.get("score") is not None:
            print(f"  Status : {r.get('status')}  Score: {r.get('score')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())