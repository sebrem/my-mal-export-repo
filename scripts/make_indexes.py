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

Dritter Ausbau (2026-11-21, Beleg Session "Season-Scan Winter 2027"):
- abo_snapshots/abo_index.json  (NEU, Flag --aboindex)
      Slim-Abo-Index: normierter Titel -> Katalog-Zugehoerigkeiten
      (mangaplus, mangaup, mangaplaza, jnc, jnc_nina, manga_million, akibapass).
      Zweck: Schritt-0-Abo-Schnelltest in EINEM kleinen Abruf, ohne die
      zu grossen Sammel-Dateien title_index.tsv/json (1,6/3 MB – Connector
      liefert sie nicht mehr) oder die einzelnen Katalog-Snapshots anfassen
      zu muessen. Matching: normalisiert EXAKT (casefold, HTML-Entities
      dekodiert, Whitespace kollabiert) – KEIN Substring (Regel aus
      titel-matching.md gilt auch hier: Substring = Fehltreffer-Risiko).
- index_manifest.json           (NEU, laeuft IMMER am Ende jedes Aufrufs)
      "Index-Index" (Index-Manifest): Registry aller Index-Dateien mit
      Zweck, Quelle, erzeugendem Flag/Workflow, Existenz, Groesse, mtime.
      Damit ist auf einen kleinen Abruf sichtbar, welche Indizes existieren
      und wie sie heissen – ohne Doku und Repo-Listing kreuzen zu muessen.

Fette Felder (Synopsen, Charaktere, externalLinks, relations, Rollen-Details,
Publisher-Notizen) bleiben bewusst NUR in den Voll-Dateien – sie werden bei
Bedarf gezielt einzeln abgerufen, nie fuer Flaechen-Checks.

Aufruf:
    python3 scripts/make_indexes.py --mal        # nur MAL-Indexe
    python3 scripts/make_indexes.py --rollcheck  # nur Rollcheck-Indexe
    python3 scripts/make_indexes.py --animedb --anilist --staff --delicenses
    python3 scripts/make_indexes.py --aboindex   # nur Abo-Slim-Index
    python3 scripts/make_indexes.py --manifest    # nur Manifest aktualisieren
    python3 scripts/make_indexes.py              # alles (inkl. Abo-Index)

Reine stdlib. Fehlt eine Quelldatei, wird ihr Index STILL UEBERSPRUNGEN
(nicht geloescht) – Workflows committen nur vorhandene Dateien.
"""
import argparse
import datetime
import html
import json
import os
import re
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
            # num_volumes_read (MAL-Baende; manga-relevant, bei anime null).
            # Grundlage fuer bandgenaue Abgleiche (JNC-Einheiten-Regel:
            # Einheit = Volume; progress = Kapitel ist dafuer unbrauchbar).
            "volumes": ls.get("num_volumes_read"),
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


# ---------------------------------------------------------------------------
# Dritter Ausbau (2026-11-21): Abo-Slim-Index + Index-Manifest
# ---------------------------------------------------------------------------

ABO_SOURCES = [
    # (Katalog-Schluessel im Index, Datei, Struktur-Art)
    ("mangaplus", "abo_snapshots/mangaplus_catalog.json", "titlemap"),
    ("mangaup", "abo_snapshots/mangaup_catalog.json", "titlemap"),
    ("mangaplaza", "abo_snapshots/mangaplaza_readinfull.json", "titlemap"),
    ("jnc", "abo_snapshots/jnc_series.json", "jnc_series"),
    ("jnc_nina", "abo_snapshots/jnc_nina_availability.json", "nina"),
    ("manga_million", "abo_snapshots/manga_million_index.json", "mm"),
    ("akibapass", "abo_snapshots/akibapass_catalog.json", "akibapass"),
]


def norm_title(t):
    """Normalisierung fuer EXAKTES Titel-Matching (kein Substring!).
    Doppelte HTML-Entity-Dekodierung (Listen enthalten teils doppelt
    kodierte ' &#x27; '), casefold, Whitespace-Kollaps."""
    t = html.unescape(html.unescape(t or ""))
    t = (t.replace("\u2019", "'").replace("\u2018", "'")
          .replace("\u2013", "-").replace("\u2014", "-"))
    t = re.sub(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff\u3005\u300c\u300d ']+",
              " ", t.casefold())
    return re.sub(r"\s+", " ", t).strip()


def abo_index():
    entries = {}

    def add(nrm, catalog, payload, display):
        if not nrm:
            return
        e = entries.setdefault(nrm, {"title": display, "catalogs": {}})
        if len(display) > len(e["title"]):
            e["title"] = display
        if payload:
            e["catalogs"][catalog] = payload

    for key, fname, art in ABO_SOURCES:
        data = load_json(os.path.join(ROOT, fname))
        if data is None:
            print("SKIP abo_index: %s (Quelldatei fehlt)" % fname)
            continue
        n_added = 0
        if art == "titlemap":
            for title, meta in (data or {}).items():
                if title.startswith("_"):
                    continue
                meta = meta or {}
                add(norm_title(title), key,
                    {"id": meta.get("title_id"), "url": meta.get("title_url"),
                     "status": meta.get("status")}, title)
                n_added += 1
        elif art == "jnc_series":
            for tkey, meta in (data or {}).items():
                title = tkey[len("[jnc] "):] if tkey.startswith("[jnc] ") else tkey
                meta = meta or {}
                add(norm_title(title), key,
                    {"slug": meta.get("slug"), "type": meta.get("type"),
                     "status": meta.get("status")}, title)
                n_added += 1
        elif art == "nina":
            for slug, meta in (data or {}).items():
                if slug.startswith("_") or not isinstance(meta, dict):
                    continue
                title = meta.get("title") or slug
                add(norm_title(title), key,
                    {"slug": slug, "status": meta.get("status")}, title)
                n_added += 1
        elif art == "mm":
            for tid, meta in ((data or {}).get("titles") or {}).items():
                meta = meta or {}
                for name in (meta.get("name"), meta.get("name_de")):
                    if not name:
                        continue
                    add(norm_title(name), key,
                        {"id": tid,
                         "de_chapters": meta.get("de_chapters"),
                         "en_chapters": meta.get("en_chapters"),
                         "de_differs": meta.get("de_differs")}, name)
                    n_added += 1
        elif art == "akibapass":
            for slug, meta in (data or {}).items():
                meta = meta or {}
                title = meta.get("title")
                if not title:
                    continue
                add(norm_title(title), key,
                    {"slug": slug, "url": meta.get("url")}, title)
                n_added += 1
        print("OK abo_index <- %s (%d Titel)" % (key, n_added))

    if not entries:
        print("SKIP abo_index.json (keine Katalog-Quellen vorhanden)")
        return None
    out = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(entries),
        "note": ("Slim-Abo-Index (normierter Titel -> Katalog-Zugehoerigkeit); "
                 "Schritt-0-Abo-Schnelltest in EINEM Abruf. Matching: "
                 "normalisiert EXAKT, kein Substring. Voll_Details -> "
                 "mangaplus/mangaup/mangaplaza- bzw. jnc-Dateien"),
        "sources": [fname for _k, fname, _a in ABO_SOURCES],
        "entries": entries,
    }
    dst = os.path.join(ROOT, "abo_snapshots", "abo_index.json")
    dump_json(dst, out)
    print("OK abo_snapshots/abo_index.json (%d normierte Titel)" % len(entries))
    return dst


# Index-Manifest ("Index-Index"): Registry aller Index-Dateien.
# (Datei, Zweck, Quelldatei(en), erzeugendes Flag, erzeugende Workflow(s))
MANIFEST_REGISTRY = [
    ("mal_anime_index.json", "Slim MAL Anime: id/Titel/Status/Score/Progress",
     "mal_anime.json", "--mal", "mal-export.yml (Mo)"),
    ("mal_manga_index.json", "Slim MAL Manga: id/Titel/Status/Score/Progress/Bde",
     "mal_manga.json", "--mal", "mal-export.yml (Mo)"),
    ("streaming_rollcheck_index.json", "mal_id -> Rollcheck-verdict/Anbieter",
     "streaming_rollcheck.json", "--rollcheck", "streaming-rollcheck.yml (taegl.)"),
    ("streaming_rollcheck_hits.json", "nur Treffer-Verdicts mit vollem result-Block",
     "streaming_rollcheck.json", "--rollcheck", "streaming-rollcheck.yml (taegl.)"),
    ("anime_db_index.json", "id -> AniList-Kern/Genres/Top-Tags",
     "anime_db.json", "--animedb", "theme-seiyuu-harvest.yml (monatl.)"),
    ("anilist_index.json", "idMal -> AniList-Status/Score/Airing",
     "anilist_dump.json", "--anilist", "anilist_export.yml (Mo)"),
    ("staff_harvest_index.json", "Aggregate + id -> Staff-Namen",
     "staff_harvest.json", "--staff", "staff-harvest.yml (monatl.)"),
    ("abo_snapshots/de_licenses_index.json", "id -> MangaUpdates-Lizenzstatus/Verlage",
     "abo_snapshots/de_licenses.json", "--delicenses", "de-license-check.yml (Mo)"),
    ("abo_snapshots/abo_index.json", "norm. Titel -> Abo-Katalog-Zugehoerigkeit (Schritt-0)",
     "7 Katalog-Snapshots (mangaplus/mangaup/mangaplaza/jnc/jnc_nina/mm/akibapass)",
     "--aboindex", "Abo-Katalog-Check (taegl.)"),
    ("mal_alias_index.json", "norm-EXAKT-Titel -> MAL-ID (Alias-Bruecke)",
     "MAL API (alias-harvest in mal-export.yml)", "--mal (mitgefuehrt)",
     "mal-export.yml + streaming-rollcheck.yml"),
    ("interest_stacks_index.json", "Stack-Ebene (141+ Stacks mit Kuraten-Daten)",
     "interest_stacks.json", "(separat)", "interest-stacks.yml (monatl.)"),
    ("interest_stacks_title_index.json", "Titel -> Stack-IDs (Rueckrichtung, open/ptw)",
     "interest_stacks.json", "(separat)", "interest-stacks.yml (monatl.)"),
    ("abo_snapshots/title_index.json",
     "VOLL-Sammelindex Titel->Kataloge (3 MB; Connector liefert NICHT, >1MB) – "
     "fuer Schritt-0 stattdessen abo_index.json nutzen",
     "Abo-Katalog-Einzelquellen", "(separat)", "Abo-Katalog-Check (taegl.)"),
    ("abo_snapshots/title_index.tsv",
     "TSV-Variante des Sammelindex (1,6 MB; Connector liefert NICHT, >1MB)",
     "Abo-Katalog-Einzelquellen", "(separat)", "Abo-Katalog-Check (taegl.)"),
]


def index_manifest():
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    indexes = []
    for fname, purpose, source, flag, workflow in MANIFEST_REGISTRY:
        path = os.path.join(ROOT, fname)
        entry = {
            "file": fname,
            "purpose": purpose,
            "source": source,
            "generated_by_flag": flag,
            "generated_by_workflow": workflow,
            "exists": os.path.isfile(path),
        }
        if entry["exists"]:
            st = os.stat(path)
            entry["size_bytes"] = st.st_size
            entry["mtime_utc"] = datetime.datetime.fromtimestamp(
                st.st_mtime, datetime.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
            # Indexe tragen 'count'/'generated_at' im Kopf – Mini-Extraktion
            # ohne Voll-Parse bei >1-MB-Dateien:
            try:
                with open(path, encoding="utf-8") as fh:
                    head = fh.read(4096)
                m = re.search(r'"count"\s*:\s*(\d+)', head)
                if m:
                    entry["record_count"] = int(m.group(1))
                g = re.search(r'"generated_at"\s*:\s*"([^"]+)"', head)
                if g:
                    entry["generated_at"] = g.group(1)
            except Exception:
                pass
        indexes.append(entry)
    out = {
        "generated_at": now,
        "note": ("Index-Manifest ('Index-Index'): Registry aller Slim-/Voll-"
                 "Index-Dateien mit Zweck/Quelle/Workflow und Live-Statistik. "
                 "Wird von JEDEM make_indexes-Lauf aktualisiert. Neuer Index "
                 "neu? -> hier UND in MANIFEST_REGISTRY des Skripts eintragen."),
        "counts": {
            "registered": len(indexes),
            "existing": sum(1 for e in indexes if e["exists"]),
        },
        "indexes": indexes,
    }
    dst = os.path.join(ROOT, "index_manifest.json")
    dump_json(dst, out)
    print("OK index_manifest.json (%d/%d Index-Dateien vorhanden)"
          % (out["counts"]["existing"], out["counts"]["registered"]))
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mal", action="store_true", help="MAL-Indexe erzeugen")
    ap.add_argument("--rollcheck", action="store_true", help="Rollcheck-Indexe erzeugen")
    ap.add_argument("--animedb", action="store_true", help="anime_db_index.json erzeugen")
    ap.add_argument("--anilist", action="store_true", help="anilist_index.json erzeugen")
    ap.add_argument("--staff", action="store_true", help="staff_harvest_index.json erzeugen")
    ap.add_argument("--delicenses", action="store_true", help="de_licenses_index.json erzeugen")
    ap.add_argument("--aboindex", action="store_true",
                    help="abo_snapshots/abo_index.json erzeugen (Abo-Slim-Index)")
    ap.add_argument("--manifest", action="store_true",
                    help="NUR index_manifest.json aktualisieren")
    args = ap.parse_args()
    if not any([args.mal, args.rollcheck, args.animedb, args.anilist,
                args.staff, args.delicenses, args.aboindex, args.manifest]):
        args.mal = True
        args.rollcheck = True
        args.animedb = True
        args.anilist = True
        args.staff = True
        args.delicenses = True
        args.aboindex = True
    if not args.manifest:  # --manifest = nur Manifest, keine Index-Neuerzeugung
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
        if args.aboindex:
            abo_index()
    index_manifest()  # laeuft IMMER mit (billig und aktuell halten)
    return 0


if __name__ == "__main__":
    sys.exit(main())
