#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_indexes.py – Slim-Index-Dateien fuer Ad-hoc-Abfragen (Vibe-Session-Schutz).

Hintergrund (2026-11-02, Beleg Session "Waiting in the Summer"):
Der GitHub-Connector liefert jede Repo-Datei IMMER komplett in den Session-Speicher
(auch wenn das Aufruf-Skript danach per ID filtert). mal_anime.json (~2 MB) und
streaming_rollcheck.json (~700 KB, wachsend mit jedem Cycle) sprengen bei
Mehrfach-Abruf das 8-MB-Session-Limit. Ad-hoc-Fragen brauchen aber nur
id/Titel/Status/Score bzw. verdict/Anbieter – dafuer dienen diese Indexe.

Erzeugt (jeweils im Repo-Root, neben den Quelldateien):
- aus mal_anime.json / mal_manga.json:
    mal_anime_index.json / mal_manga_index.json
    Eintraege: id, title, status, score, progress (Ep./Kap.), updated_at
- aus streaming_rollcheck.json:
    streaming_rollcheck_index.json
      entries[<mal_id>] = {title, mal_status, checked_at, verdict,
                           match_confidence, providers[...]}  (+ Kopf-Metadaten)
    streaming_rollcheck_hits.json
      nur Treffer-Verdicts (hit_* / *_caveat / de_legal_only) mit vollem
      result-Block – kleine Datei fuer Monitoring-/Lane-Checks.

Zweiter Ausbau (2026-11-02, Nachfrage "weitere Indizes"):
- aus anime_db.json:        anime_db_index.json (id -> Titel, AniList-Kern, Genres, Top-Tags)
- aus anilist_dump.json:     anilist_index.json  (idMal -> AniList-Status/Score/Genres/Airing)
- aus staff_harvest.json:    staff_harvest_index.json (Aggregate + id -> Staff-Namen)
- aus abo_snapshots/de_licenses.json: abo_snapshots/de_licenses_index.json
                             (id -> Titel, mu_licensed, mu_status, Verlagsnamen)
Fette Felder (Synopsen, Charaktere, externalLinks, relations, Rollen-Details,
Publisher-Notizen) bleiben bewusst NUR in den Voll-Dateien – sie werden bei
Bedarf gezielt einzeln abgerufen, nie fuer Flaechen-Checks.

Aufruf:
    python3 scripts/make_indexes.py --mal        # nur MAL-Indexe
    python3 scripts/make_indexes.py --rollcheck  # nur Rollcheck-Indexe
    python3 scripts/make_indexes.py --animedb --anilist --staff --delicenses
    python3 scripts/make_indexes.py              # alles

Reine stdlib. Fehlt eine Quelldatei, wird ihr Index STILL UEBERSPRUNGEN
(nicht geloescht) – Workflows committen nur vorhandene Dateien.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HIT_VERDICTS = {
    "hit_direct",
    "hit_channel",
    "hit_channel_candidate",
    "hit_cr_channel_caveat",
    "hit_adn_channel_caveat",
    "de_legal_only",
}

PROVIDER_LIST_KEYS = (
    "my_direct_hits",
    "my_channel_hits",
    "channel_candidates",
    "crunchyroll_channel_caveat",
    "adn_channel_caveat",
    "legal_de_other_providers",
    "buy_physical_offers",
)


def load_json(path):
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")


def mal_index(kind):
    src = os.path.join(ROOT, "mal_%s.json" % kind)
    data = load_json(src)
    if data is None:
        print("SKIP mal_%s_index.json (Quelldatei fehlt)" % kind)
        return None
    entries = []
    for item in data.get("list", []):
        ls = item.get("list_status") or {}
        progress = ls.get("num_episodes_watched")
        if progress is None:
            progress = ls.get("num_chapters_read")
        entries.append({
            "id": item.get("id"),
            "title": item.get("title"),
            "status": ls.get("status"),
            "score": ls.get("score"),
            "progress": progress,
            "updated_at": ls.get("updated_at"),
        })
    out = {
        "generated_at": data.get("generated_at"),
        "source": data.get("source"),
        "user": data.get("user"),
        "kind": kind,
        "count": len(entries),
        "note": "Slim-Index fuer Ad-hoc-Statusverifikation; Detailfelder -> mal_%s.json" % kind,
        "list": entries,
    }
    dst = os.path.join(ROOT, "mal_%s_index.json" % kind)
    dump_json(dst, out)
    print("OK %s (%d Eintraege)" % (os.path.basename(dst), len(entries)))
    return dst


def provider_names(result):
    names = []
    for key in PROVIDER_LIST_KEYS:
        for hit in result.get(key) or []:
            code = hit.get("provider")
            name = hit.get("name") or ""
            label = "%s(%s)" % (code, key.split("_")[0]) if code else name
            if label and label not in names:
                names.append(label)
    return names


def rollcheck_index():
    src = os.path.join(ROOT, "streaming_rollcheck.json")
    data = load_json(src)
    if data is None:
        print("SKIP Rollcheck-Indexe (Quelldatei fehlt)")
        return None
    index_entries = {}
    hits = {}
    for mal_id, entry in (data.get("cycle_hits") or {}).items():
        verdict = entry.get("verdict")
        index_entries[mal_id] = {
            "title": entry.get("title"),
            "mal_status": entry.get("mal_status"),
            "checked_at": entry.get("checked_at"),
            "verdict": verdict,
            "match_confidence": entry.get("match_confidence"),
            "providers": provider_names(entry),
        }
        if verdict in HIT_VERDICTS:
            hits[mal_id] = entry
    # Aktueller Chunk (falls Titel noch nicht in cycle_hits uebernommen):
    for item in data.get("chunk_results") or []:
        mal_id = str(item.get("mal_id"))
        if mal_id in index_entries:
            continue
        result = item.get("result") or {}
        index_entries[mal_id] = {
            "title": item.get("title"),
            "mal_status": item.get("status"),
            "checked_at": data.get("run"),
            "verdict": result.get("verdict"),
            "match_confidence": result.get("match_confidence"),
            "providers": provider_names(result),
        }
        if result.get("verdict") in HIT_VERDICTS:
            hits[mal_id] = {
                "title": item.get("title"),
                "mal_status": item.get("status"),
                "checked_at": data.get("run"),
                "verdict": result.get("verdict"),
                "result": result,
            }
    index = {
        "run": data.get("run"),
        "cycle": data.get("cycle"),
        "cycle_progress": data.get("cycle_progress"),
        "cycle_hits_summary": data.get("cycle_hits_summary"),
        "note": "Slim-Index (mal_id -> verdict/Anbieter); Treffer-Details -> streaming_rollcheck_hits.json",
        "entries": index_entries,
    }
    dst_index = os.path.join(ROOT, "streaming_rollcheck_index.json")
    dst_hits = os.path.join(ROOT, "streaming_rollcheck_hits.json")
    dump_json(dst_index, index)
    dump_json(dst_hits, {
        "run": data.get("run"),
        "note": "Nur Treffer-Verdicts (%s)" % ", ".join(sorted(HIT_VERDICTS)),
        "entries": hits,
    })
    print("OK streaming_rollcheck_index.json (%d Eintraege)" % len(index_entries))
    print("OK streaming_rollcheck_hits.json (%d Treffer)" % len(hits))
    return dst_index


ANIME_DB_TAG_LIMIT = 8


def animedb_index():
    src = os.path.join(ROOT, "anime_db.json")
    data = load_json(src)
    if data is None:
        print("SKIP anime_db_index.json (Quelldatei fehlt)")
        return None
    entries = {}
    for t in data.get("titles", []):
        an = t.get("anilist") or {}
        tags = [
            {"name": tg.get("name"), "rank": tg.get("rank")}
            for tg in (an.get("tags") or [])[:ANIME_DB_TAG_LIMIT]
        ]
        entries[str(t.get("id"))] = {
            "title": t.get("title"),
            "romaji": an.get("romaji"),
            "english": an.get("english"),
            "format": an.get("format"),
            "episodes": an.get("episodes"),
            "seasonYear": an.get("seasonYear"),
            "averageScore": an.get("averageScore"),
            "genres": an.get("genres") or [],
            "tags": tags,
        }
    out = {
        "generated_at": data.get("generated_at"),
        "count": len(entries),
        "note": "Slim-Index (id -> Kern-Metadaten/Genres/Tags); Synopsen+Charaktere -> anime_db.json",
        "entries": entries,
    }
    dst = os.path.join(ROOT, "anime_db_index.json")
    dump_json(dst, out)
    print("OK anime_db_index.json (%d Eintraege)" % len(entries))
    return dst


def anilist_index():
    src = os.path.join(ROOT, "anilist_dump.json")
    data = load_json(src)
    if data is None:
        print("SKIP anilist_index.json (Quelldatei fehlt)")
        return None
    entries = {}
    for kind in ("anime", "manga"):
        for item in data.get(kind) or []:
            mal_id = item.get("idMal")
            if not mal_id:
                continue
            na = item.get("nextAiringEpisode") or {}
            entries[str(mal_id)] = {
                "kind": kind,
                "anilist_id": item.get("id"),
                "al_status": item.get("status"),
                "format": item.get("format"),
                "averageScore": item.get("averageScore"),
                "genres": item.get("genres") or [],
                "next_episode": na.get("episode"),
                "next_airing_at": na.get("airingAt"),
            }
    out = {
        "generated_at": data.get("generated_at"),
        "count": len(entries),
        "unmapped_count": len(data.get("unmapped") or []),
        "note": "Slim-Index (idMal -> AniList-Status/Score/Airing); externalLinks/relations -> anilist_dump.json",
        "entries": entries,
    }
    dst = os.path.join(ROOT, "anilist_index.json")
    dump_json(dst, out)
    print("OK anilist_index.json (%d Eintraege)" % len(entries))
    return dst


def _names(lst):
    return [p.get("name") for p in (lst or []) if p.get("name")]


def staff_index():
    src = os.path.join(ROOT, "staff_harvest.json")
    data = load_json(src)
    if data is None:
        print("SKIP staff_harvest_index.json (Quelldatei fehlt)")
        return None
    def slim(items):
        out = {}
        for it in items or []:
            out[str(it.get("id"))] = {
                "title": it.get("title"),
                "score": it.get("score"),
                "status": it.get("status"),
                "mangaka": _names(it.get("mangaka")),
                "original_story": _names(it.get("original_story")),
                "studios": it.get("studios") or [],
                "directors": _names(it.get("directors")),
                "chara_design": _names(it.get("chara_design")),
                "original_creator": _names(it.get("original_creator")),
                "series_composition": _names(it.get("series_composition")),
            }
        return out
    out = {
        "generated_at": data.get("generated_at"),
        "count_manga": data.get("count_manga"),
        "count_anime": data.get("count_anime"),
        "aggregates": data.get("aggregates"),
        "note": "Slim-Index (Aggregate + id -> Staff-NAMEN ohne Rollen); Rollen-Details -> staff_harvest.json",
        "manga": slim(data.get("manga")),
        "anime": slim(data.get("anime")),
    }
    dst = os.path.join(ROOT, "staff_harvest_index.json")
    dump_json(dst, out)
    print("OK staff_harvest_index.json (%d Manga, %d Anime)"
          % (len(out["manga"]), len(out["anime"])))
    return dst


def delicenses_index():
    src = os.path.join(ROOT, "abo_snapshots", "de_licenses.json")
    data = load_json(src)
    if data is None:
        print("SKIP de_licenses_index.json (Quelldatei fehlt)")
        return None
    entries = {}
    for mal_id, t in (data.get("titles") or {}).items():
        entries[str(mal_id)] = {
            "title": t.get("title"),
            "mal_status": t.get("status"),
            "mu_licensed": t.get("mu_licensed"),
            "mu_status": t.get("mu_status"),
            "publishers": [
                {"name": p.get("name"), "type": p.get("type")}
                for p in (t.get("publishers") or [])
            ],
            "last_checked": (t.get("last_checked") or "")[:10],
        }
    out = {
        "generated_at": data.get("generated_at"),
        "count": len(entries),
        "note": "Slim-Index (id -> Lizenzstatus/Verlagsnamen); Notes/mu_series_id -> de_licenses.json",
        "entries": entries,
    }
    dst = os.path.join(ROOT, "abo_snapshots", "de_licenses_index.json")
    dump_json(dst, out)
    print("OK de_licenses_index.json (%d Eintraege)" % len(entries))
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mal", action="store_true", help="MAL-Indexe erzeugen")
    ap.add_argument("--rollcheck", action="store_true", help="Rollcheck-Indexe erzeugen")
    ap.add_argument("--animedb", action="store_true", help="anime_db_index.json erzeugen")
    ap.add_argument("--anilist", action="store_true", help="anilist_index.json erzeugen")
    ap.add_argument("--staff", action="store_true", help="staff_harvest_index.json erzeugen")
    ap.add_argument("--delicenses", action="store_true", help="de_licenses_index.json erzeugen")
    args = ap.parse_args()
    if not any([args.mal, args.rollcheck, args.animedb, args.anilist,
                args.staff, args.delicenses]):
        args.mal = True
        args.rollcheck = True
        args.animedb = True
        args.anilist = True
        args.staff = True
        args.delicenses = True
    if args.mal:
        mal_index("anime")
        mal_index("manga")
    if args.rollcheck:
        rollcheck_index()
    if args.animedb:
        animedb_index()
    if args.anilist:
        anilist_index()
    if args.staff:
        staff_index()
    if args.delicenses:
        delicenses_index()
    return 0


if __name__ == "__main__":
    sys.exit(main())
