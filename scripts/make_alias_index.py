#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_alias_index.py – mal_alias_index.json: Titelvarianten-Index (id -> Aliase).

Umbenennung 12.11.2026 (Möbiusschleife): hieß make_aliases.py, Ausgabe hieß
mal_aliases.json – kollidierte mit dem UNABHÄNGIGEN, älteren CLI-Resolver
scripts/mal_aliases.py (open_titles.json -> open_title_hints.json, LIVE seit
04.11.). Neue Namen: Script make_alias_index.py, Datendatei mal_alias_index.json.

Hintergrund (2026-11-11, Beleg "Net-juu no Susume", MAL 36038):
MAL-Schreibweisen variieren (Net-juu / Netojuu, Bindestrich ja/nein) und
Ad-hoc-Suchen per Titel scheitern an exakter Schreibweise. Die Duo-Discovery-
Regel (Skill anime-manga-abgleich) referenziert den Alias-Index als
id->Titelvarianten-Quelle. Quellen:
  1) mal_anime.json / mal_manga.json: Feld "title" + "alternative_titles"
     (en/ja/synonyms; im MAL-Export seit Patch 2026-11-11)
  2) anilist_dump.json: title{romaji english native} + synonyms
     (synonyms im Dump seit FIELDS-Patch 2026-11-11; alter Dump ohne
     synonyms wird still ignoriert)

ROLLE (Möbiusschleife 12.11.2026, Abgrenzung zur führenden Instanz):
  Fuehrende Instanz fuer Titel-Lookups bleibt abo_snapshots/title_index.tsv
  bzw. scripts/title_lookup.py (Fuzzy + Staleness-Fallback). mal_alias_index.json
  ist die Synonym-/Varianten-VOLLDATENQUELLE mit norm-Keys fuer Duo-Discovery-
  und Synonym-Greps; sein Synonym-Vorteil soll per Nachtrag in title_index.py
  einfliesen, danach kann dieser Index in den Ruhestand.

Ausgabe-Format (Repo-Root):
  mal_alias_index.json:
    { generated_at, source, note, count,
      entries: { "<mal_id>": { "main": str, "variants": [...roh, dedup],
                               "norm": [...normalisiert, dedup] } } }

MATCHING-SEMANTIK: 'norm' = lowercase, nur [a-z0-9\u3040-\u30ff\u4e00-\u9fff]
(alles andere inkl. Bindestrich/Leerzeichen/Punkt raus). EXAKTER Vergleich
gegen norm-Elemente - KEIN Substring-Match (Beleg: "pardon the intrusion"
trifft nicht, norm-Element ist "pardontheintrusionimhome"; Vollvariante
"Pardon the Intrusion, I'm Home!" trifft). Fehlende Quellen werden still
uebersprungen.

Aufruf: python3 scripts/make_alias_index.py
"""
import json
import os
import re
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = "mal_alias_index.json"

# CJK-Kana/Kanji behalten, sonst nur a-z0-9
_KEEP = re.compile(r"[^a-z0-9\u3040-\u30ff\u4e00-\u9fff]")


def normalize(s):
    if not s:
        return ""
    return _KEEP.sub("", s.lower()).strip()


def load_json(path):
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _absorb(entry, main, candidates):
    """Kandidaten dedupiziert einsammeln (case-insensitiv, Normalform)."""
    if main and not entry["main"]:
        entry["main"] = main
    seen_norm = set(entry["norm"])
    for c in candidates:
        if not c or not isinstance(c, str):
            continue
        if c not in entry["variants"]:
            entry["variants"].append(c)
        n = normalize(c)
        if n and n not in seen_norm:
            seen_norm.add(n)
            entry["norm"].append(n)


def mal_variants(kind, entries):
    """Titel + alternative_titles aus mal_<kind>.json einsammeln."""
    data = load_json(os.path.join(ROOT, "mal_%s.json" % kind))
    if data is None:
        print("SKIP mal_%s.json (fehlt)" % kind)
        return
    for item in data.get("list", []):
        mal_id = str(item.get("id"))
        e = entries.setdefault(mal_id, {"main": None, "variants": [], "norm": []})
        at = item.get("alternative_titles") or {}
        cands = [item.get("title"), at.get("en"), at.get("ja")]
        cands += at.get("synonyms") or []
        _absorb(e, item.get("title"), cands)


def anilist_variants(entries):
    """title{romaji english native} + synonyms aus anilist_dump.json."""
    data = load_json(os.path.join(ROOT, "anilist_dump.json"))
    if data is None:
        print("SKIP anilist_dump.json (fehlt)")
        return
    for kind in ("anime", "manga"):
        for item in data.get(kind) or []:
            mal_id = item.get("idMal")
            if not mal_id:
                continue
            e = entries.setdefault(str(mal_id), {"main": None, "variants": [], "norm": []})
            title = item.get("title") or {}
            cands = [title.get("romaji"), title.get("english"), title.get("native")]
            cands += item.get("synonyms") or []
            _absorb(e, None, cands)


def main():
    entries = {}
    mal_variants("anime", entries)
    mal_variants("manga", entries)
    anilist_variants(entries)

    # Aufraeumen: Eintraege ganz ohne Varianten raus
    entries = {k: v for k, v in entries.items() if v["variants"]}
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "mal_anime.json/mal_manga.json (alternative_titles) + anilist_dump.json (title, synonyms)",
        "note": "id -> Titelvarianten; 'norm' = normalisiert (lowercase, Sonderzeichen raus), EXAKT zu matchen (kein Substring); fuehrende Lookup-Instanz bleibt title_index.tsv",
        "count": len(entries),
        "entries": entries,
    }
    with open(os.path.join(ROOT, OUT), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
    print("OK %s (%d Eintraege)" % (OUT, len(entries)))


if __name__ == "__main__":
    main()
