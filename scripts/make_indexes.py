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

Aufruf:
    python3 scripts/make_indexes.py --mal        # nur MAL-Indexe
    python3 scripts/make_indexes.py --rollcheck  # nur Rollcheck-Indexe
    python3 scripts/make_indexes.py              # beides

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mal", action="store_true", help="MAL-Indexe erzeugen")
    ap.add_argument("--rollcheck", action="store_true", help="Rollcheck-Indexe erzeugen")
    args = ap.parse_args()
    if not (args.mal or args.rollcheck):
        args.mal = True
        args.rollcheck = True
    if args.mal:
        mal_index("anime")
        mal_index("manga")
    if args.rollcheck:
        rollcheck_index()
    return 0


if __name__ == "__main__":
    sys.exit(main())
