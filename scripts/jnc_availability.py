#!/usr/bin/env python3
"""
JNC-Verfuegbarkeits-Lane (Stand 14.11.2026, erste Iteration).
Fragt die JNC-API v2 (labs.j-novel.club) ab und dokumentiert pro Serie:
  - library:        mind. ein Volume mit readerStreamingEnabled: true
  - prepub_active:  Volumes mit publishing-Datum in der Zukunft oder
                    <= 32 Tage zurueck (Pre-Pubs laufen ~1 Monat nach
                    E-Book-Release ab, "expired" meist um den 15.)
WICHTIG (blank-UA-Pflicht): Die API antwortet nur mit LEEREM
User-Agent (explizit ueberschrieben) und Accept: application/json;
Browser-/Python-Standard-UA = 403.
Das Serien-Feld "catchup" ist UNZUVERLAESSIG (Slayers: false trotz
Library) - Library-Status wird nur ueber readerStreamingEnabled pro
Volume bestimmt. Katalog-Listung != Lesbarkeits-Nachweis
(Belegfall: Dagashi-ya Yahagi).
Snapshots: abo_snapshots/jnc_availability.json
Diff (new/removed/changed): Merge nach abo_changes.json["jnc_availability"].
Bandgenau (Einheiten-Regel 14.11.2026: JNC-Einheit = VOLUME):
pro Serie "library_vols": [Bandnummern] und "prepub_active":
[{number, publishing, readerStreamingEnabled}] - Grundlage fuer den
Anschluss-Join (scripts/jnc_anschluss.py x abo_snapshots/own_volumes.json).
Laeuft im Abo-Katalog-Check-Workflow als eigener Step nach den
Katalog-Lanes (continue-on-error: true).
Bewusst reine stdlib (urllib+json) - keine requests noetig.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(os.environ.get("REPO_ROOT", str(Path.cwd())))
SNAP_DIR = REPO / "abo_snapshots"
SNAP_DIR.mkdir(exist_ok=True)
SNAP_PATH = SNAP_DIR / "jnc_availability.json"
CHANGES_PATH = REPO / "abo_changes.json"
API = "https://labs.j-novel.club/app/v2"
PREPUB_WINDOW_DAYS = 32   # publishing <= 32 Tage zurueck gilt als aktiv


def api_get(url):
    # blank-UA-Pflicht: explizit leerer User-Agent, sonst 403
    req = urllib.request.Request(url, headers={
        "User-Agent": "",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all_series():
    series, skip = [], 0
    while True:
        batch = api_get(f"{API}/series?format=json&limit=500&skip={skip}")
        items = batch.get("series", batch if isinstance(batch, list) else [])
        if not items:
            break
        series.extend(items)
        if len(items) < 500:
            break
        skip += 500
    return series


def fetch_volumes(slug):
    volumes, skip = [], 0
    while True:
        batch = api_get(
            f"{API}/series/{slug}/volumes?format=json&limit=500&skip={skip}")
        items = batch.get("volumes", batch if isinstance(batch, list) else [])
        if not items:
            break
        volumes.extend(items)
        if len(items) < 500:
            break
        skip += 500
    return volumes


def main():
    now = datetime.now(timezone.utc)
    series_list = fetch_all_series()
    if not series_list:
        print("jnc_availability: 0 Serien erkannt - API pruefen")
        return 1

    current = {}
    for i, s in enumerate(series_list, 1):
        slug = s.get("slug") or s.get("id")
        if not slug:
            continue
        try:
            vols = fetch_volumes(slug)
        except Exception as e:
            print(f"  [{i}/{len(series_list)}] {slug}: Volume-Fetch "
                  f"fehlgeschlagen ({e}) - Serie uebersprungen")
            continue
        library = any(v.get("readerStreamingEnabled") for v in vols)
        prepub = []
        for v in vols:
            pub = v.get("publishing")
            if not pub:
                continue
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except ValueError:
                continue
            if pub_dt > now or (now - pub_dt).days <= PREPUB_WINDOW_DAYS:
                prepub.append({
                    "number": v.get("number"),
                    "title": v.get("title", ""),
                    "publishing": pub,
                    "readerStreamingEnabled": bool(v.get("readerStreamingEnabled")),
                })
        lib_nums = sorted(n for n in (v.get("number") for v in vols
                            if v.get("readerStreamingEnabled")) if n is not None)
        current[slug] = {
            "title": s.get("title", ""),
            "status": s.get("status", ""),
            "library": library,
            "library_vols": lib_nums,
            "prepub_active": prepub,
        }
        if i % 50 == 0:
            print(f"  ... {i}/{len(series_list)} Serien verarbeitet")

    # Diff analog mangaup_catalog.py
    old = {}
    if SNAP_PATH.exists():
        try:
            old = json.loads(SNAP_PATH.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    new_keys = sorted(k for k in current if k not in old)
    removed = sorted(k for k in old if k not in current)
    changed = sorted(k for k in current
                     if k in old and old[k] != current[k])
    SNAP_PATH.write_text(
        json.dumps(current, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8")

    delta = {
        "new": {k: current[k] for k in new_keys},
        "removed": removed,
        "changed": {k: {"old": old[k], "new": current[k]}
                    for k in changed},
    }
    changes = {}
    if CHANGES_PATH.exists():
        try:
            changes = json.loads(CHANGES_PATH.read_text(encoding="utf-8"))
        except Exception:
            changes = {}
    changes["jnc_availability"] = delta
    CHANGES_PATH.write_text(
        json.dumps(changes, ensure_ascii=False, indent=1),
        encoding="utf-8")

    n_lib = sum(1 for v in current.values() if v["library"])
    n_pub = sum(1 for v in current.values() if v["prepub_active"])
    print(f"JNC-Verfuegbarkeit: {len(current)} Serien "
          f"(Library: {n_lib}, Pre-Pub aktiv: {n_pub}; "
          f"neu: {len(new_keys)}, entfernt: {len(removed)}, "
          f"geaendert: {len(changed)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
