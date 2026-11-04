#!/usr/bin/env python3
"""
title_lookup.py v2 (Entwurf 04.11.2026) - Titel-Matching-Werkzeugkasten als
ausfuehrbares Werkzeug. Aufloesung BELIEBIGER Schreibweise (Kanji, Romaji
mit/ohne Macron, EN, Synonym) zu MAL-ID, Medium und allen bekannten Aliasen.

DATEN-SCHICHTUNG mit Fallback (Overengineering-Krönung, Sebastian-Wunsch):
  1. FAST PATH: abo_snapshots/title_index.json (GitHub Action title_index)
     - NUR wenn FRISCH: index.generated_at >= generated_at ALLER
       vorhandenen Quellen. Vergleich gegen die QUELLEN, nicht gegen die
       Uhr - GitHub-Delays/gescheiterte Laeufe machen Altersschwellen
       unzuverlaessig; veraltbar ist ein Index genau dann, wenn eine
       Quelle NEUER ist als er (z.B. mal-export 04:17 gelaufen,
       title_index 04:50 gescheitert -> DELAY-Flip bis zum naechsten Lauf).
  2. FALLBACK (stale/fehlt/unlesbar/0 Records): Index on-the-fly aus
     mal_manga.json + mal_anime.json + anilist_dump.json bauen.
  Der jeweils aktive Pfad wird im Output angegeben (Transparenz fuer
  Agent-Session und Task-Logs: "Index: Snapshot (frisch)" vs.
  "Index: Fallback on-the-fly (Snapshot fehlt/stale)").

Normalisierungs-Kette (faengt die bekannten Fallen ab):
  1. NFKC                 -> Vollbreite-Ziffern/Zeichen (Amazon "Vol.１０")
  2. NFKD + Akzent-Fold   -> Macron/Umlaut-Varianten (Kokou/Kokō, û/uu)
  3. lower + strip        -> Gross/Kleinschreibung, Leerzeichen
  4. Satzzeichen raus      -> Bindestrich-Fallen (Bijutsu-bu/Bijutsubu)
  5. optional "squash"    -> ALLE Nicht-Alphanumerika raus (Fuzzy-Stufe 2)

Aufruf-Beispiele:
  python3 scripts/title_lookup.py "孤高なカノジョと、彼女の部屋でシてること"
  python3 scripts/title_lookup.py "Kokou na Kanojo"             # Teilstring
  python3 scripts/title_lookup.py --medium anime "Kono Bijutsu"
  python3 scripts/title_lookup.py --squash "bijutsubu"          # Stufe 2
  python3 scripts/title_lookup.py --status                       # nur Index-
                                     Frische melden (fuer Task-Praeluden)

Exit-Codes: 0 = Treffer, 1 = kein Treffer, 2 = keine Query.
Teilstring-Match ab 4 normalisierten Zeichen (kein Match gegen "no"/"wa").
Doku: knowledge/anime-manga/titel-matching.md (fuehrende Instanz).
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
INDEX_PATH = REPO / "abo_snapshots" / "title_index.json"
MIN_LEN = 4
SQUASH_MIN_LEN = 5


# ---------------------------------------------------------------- Normierung

def norm(t):
    """NFKC -> Macron/Umlaut-Fold -> lower -> Satzzeichen raus."""
    if not t:
        return ""
    s = unicodedata.normalize("NFKC", str(t))
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = "".join(c if c.isalnum() or c == " " else " " for c in s)
    return " ".join(s.split())


def squash(t):
    """Fuzzy-Stufe 2: ALLE Nicht-Alphanumerika raus (Bijutsu-bu=Bijutsubu)."""
    return "".join(c for c in norm(t) if c.isalnum())


def _gen_date(path):
    """generated_at einer Repo-JSON lesen (Feld heisst je Quelle anders)."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return str(d.get("generated_at")
                   or d.get("generated_at_utc") or "").replace("Z", "")
    except Exception:
        return ""


# ------------------------------------------------------------ Index-Aufbau

def _add(index, key, rec):
    if not key or len(key) < 2:
        return
    for existing in index.get(key, []):
        if (existing.get("mal_id") == rec.get("mal_id")
                and existing.get("medium") == rec.get("medium")):
            # Merge: fehlende Felder ergaenzen (z.B. format aus AniList)
            for f in ("anilist_id", "format", "romaji", "english",
                      "japanese", "status", "score"):
                if rec.get(f) is not None and existing.get(f) is None:
                    existing[f] = rec.get(f)
            return
    index.setdefault(key, []).append(rec)


def _rec_from_index(rec):
    """Snapshot-Record -> einheitliche Record-Form fuer Ausgabe."""
    return {"mal_id": rec.get("mal_id"), "medium": rec.get("medium"),
            "anilist_id": rec.get("anilist_id"),
            "format": rec.get("format"),
            "quelle": "index_snapshot",
            "romaji": rec.get("title_romaji") or rec.get("title_mal"),
            "english": rec.get("title_english"),
            "japanese": rec.get("title_native"),
            "status": rec.get("status"), "score": rec.get("score")}


def build_index_from_snapshot():
    """Snapshot-Records -> key-Index; None bei unlesbar/leer."""
    if not INDEX_PATH.exists():
        return None, "Snapshot fehlt"
    try:
        with open(INDEX_PATH, encoding="utf-8") as f:
            snap = json.load(f)
    except Exception as e:
        return None, f"Snapshot unlesbar: {e}"
    records = snap.get("records") or []
    if not records:
        return None, "Snapshot leer (0 Records)"
    index = {}
    for rec in records:
        r = _rec_from_index(rec)
        for key in rec.get("keys") or []:
            _add(index, key, r)
    return index, None


def build_index_from_sources(medium_filter=None):
    """Fallback: Index on-the-fly aus MAL-Exporten + AniList-Dump."""
    index = {}

    # AniList: romaji/english/native/synonyms je idMal
    anilist_by_mal = {}
    if ANILIST_PATH.exists():
        with open(ANILIST_PATH, encoding="utf-8") as f:
            dump = json.load(f)
        for medium in ("manga", "anime"):
            if medium_filter and medium != medium_filter:
                continue
            for e in dump.get(medium, []):
                if not isinstance(e, dict) or not e.get("idMal"):
                    continue
                t = e.get("title") or {}
                if not isinstance(t, dict):
                    continue
                anilist_by_mal.setdefault((medium, int(e["idMal"])), {
                    "anilist_id": e.get("id"), "format": e.get("format"),
                    "romaji": t.get("romaji"), "english": t.get("english"),
                    "japanese": t.get("native"),
                    "synonyms": [s for s in (e.get("synonyms") or [])
                                 if isinstance(s, str)]})

    for path, medium in ((MAL_MANGA_PATH, "manga"), (MAL_ANIME_PATH, "anime")):
        if medium_filter and medium != medium_filter:
            continue
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            entries = json.load(f).get("list", [])
        for e in entries:
            mal_id = e.get("id")
            if not mal_id:
                continue
            ls = e.get("list_status") or {}
            al = anilist_by_mal.get((medium, int(mal_id)))
            base = {"mal_id": mal_id, "medium": medium, "quelle": "on_the_fly",
                    "anilist_id": al["anilist_id"] if al else None,
                    "format": al["format"] if al else None,
                    "romaji": e.get("title"),
                    "english": al["english"] if al else None,
                    "japanese": al["japanese"] if al else None,
                    "status": ls.get("status"), "score": ls.get("score")}
            variants = [e.get("title")]
            if al:
                variants += [al["romaji"], al["english"], al["japanese"],
                             *al["synonyms"]]
            for v in variants:
                if v and isinstance(v, str):
                    _add(index, norm(v), dict(base))
    return index


def load_index(medium_filter=None):
    """Schichtung: frischer Snapshot > Fallback on-the-fly.

    Frische-Kriterium: index.generated_at_utc >= generated_at ALLER
    vorhandenen Quellen (string-Vergleich ISO-8601-UTC). Fehlt das Feld
    auf einer Seite, gilt der Index als stale -> konservativ Fallback.
    Rueckgabe: (index, modus) mit modus fuer die Output-Praelude.
    """
    index, err = build_index_from_snapshot()
    if index is not None:
        idx_gen = _gen_date(INDEX_PATH)
        src_gens = {p.name: _gen_date(p)
                    for p in (MAL_MANGA_PATH, MAL_ANIME_PATH, ANILIST_PATH)
                    if p.exists()}
        stale = [f"{name} ({g} > {idx_gen})"
                 for name, g in src_gens.items()
                 if not idx_gen or (g and g > idx_gen)]
        if not stale:
            return index, "Snapshot (frisch)"
        err = "Snapshot stale, neuere Quellen: " + ", ".join(stale)
    return build_index_from_sources(medium_filter), \
        f"Fallback on-the-fly ({err})"


# ------------------------------------------------------------------- Lookup

def merge_records(records):
    by_key = {}
    for r in records:
        k = (r.get("mal_id"), r.get("medium"))
        if k not in by_key:
            by_key[k] = {"mal_id": r.get("mal_id"), "medium": r.get("medium"),
                         "anilist_id": r.get("anilist_id"),
                         "format": r.get("format"),
                         "quellen": set(), "romaji": None, "english": None,
                         "japanese": None, "status": None, "score": None}
        m = by_key[k]
        m["quellen"].add(r.get("quelle"))
        for f in ("romaji", "english", "japanese", "status", "score",
                   "anilist_id", "format"):
            if r.get(f) is not None and m.get(f) is None:
                m[f] = r.get(f)
    return list(by_key.values())


def lookup(query, medium_filter=None, use_squash=False):
    nq = norm(query)
    if len(nq) < MIN_LEN:
        return [], [], f"Query zu kurz (min {MIN_LEN} normalisierte Zeichen)"
    index, modus = load_index(medium_filter)

    # Stufe 1: exakt, mit Alias-Erweiterung (Geschwister-IDs wie MAL
    # 194548/194549 mit identischem Titel, aber anderem Key)
    exact = index.get(nq)
    if exact:
        seen, queue, hits = set(), list(exact), []
        while queue:
            r = queue.pop(0)
            key_id = id(r)
            if key_id in seen:
                continue
            seen.add(key_id)
            hits.append(r)
            for alias in (r.get("romaji"), r.get("english"),
                          r.get("japanese")):
                if not alias:
                    continue
                for k in (norm(alias), alias):
                    for sib in index.get(k, []):
                        if id(sib) not in seen:
                            queue.append(sib)
        return merge_records(hits), modus, "exakt (mit Alias-Erweiterung)"

    # Stufe 1b: Teilstring
    hits = []
    for key, recs in index.items():
        if nq in key:
            hits.extend(recs)
    if hits:
        return merge_records(hits), modus, "Teilstring (normalisiert)"

    # Stufe 2: squash-Fuzzy (opt-in)
    if use_squash:
        sq = squash(query)
        if len(sq) >= SQUASH_MIN_LEN:
            hits = [recs for key, recs in index.items()
                    if sq in squash(key) or sq == squash(key)]
            flat = [r for h in hits for r in h]
            if flat:
                return merge_records(flat), modus, "squash-Fuzzy"
    return [], modus, "kein Treffer (Stufen 1-2)"


def main():
    args = sys.argv[1:]
    use_squash = "--squash" in args
    args = [a for a in args if a != "--squash"]
    medium_filter = None
    if "--medium" in args:
        i = args.index("--medium")
        medium_filter = args[i + 1]
        args = args[:i] + args[i + 2:]

    # --status: nur Index-Frische melden (Task-Praeluden-Check)
    if "--status" in args:
        args = [a for a in args if a != "--status"]
        if args:
            print("--status akzeptiert keine Query")
            return 2
        _, modus = load_index()
        print(f"Index: {modus}")
        return 0

    if not args:
        print(__doc__)
        return 2
    query = " ".join(args)

    records, modus, mode = lookup(query, medium_filter, use_squash)
    print(f"Index: {modus}")
    print(f"Query: {query}")
    print(f"Match-Modus: {mode}")
    if not records:
        print("KEIN TREFFER - Negativ-Befund nur gueltig, wenn auch "
              "andere Schreibweisen (s. titel-matching.md) geprueft wurden.")
        return 1
    records.sort(key=lambda r: (r.get("mal_id") or 0))
    for r in records:
        print(f"\nMAL-ID {r['mal_id']}  [{r['medium']}]"
              + (f"  format={r.get('format')}" if r.get("format")
                 else "  format=unbekannt")
              + f"  (Quellen: {', '.join(sorted(r['quellen']))})")
        print(f"  Romaji : {r.get('romaji')}")
        print(f"  EN     : {r.get('english')}")
        print(f"  JP     : {r.get('japanese')}")
        if r.get("status") or r.get("score") is not None:
            print(f"  Status : {r.get('status')}  Score: {r.get('score')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())