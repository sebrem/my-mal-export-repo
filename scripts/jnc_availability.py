#!/usr/bin/env python3
"""
JNC-Verfuegbarkeits-Lane (Stand 14.11.2026, erste Iteration).
Fragt die JNC-API v2 (labs.j-novel.club) ab und dokumentiert pro Serie:
  - library:        mind. ein Volume mit readerStreamingEnabled: true
  - prepub_active:  Volumes mit publishing-Datum in der ZUKUNFT
                    (= digital noch nicht erschienen, aber PP-lesbar aktiv).
                    SEMANTIK KORRIGIERT 20.11.2026 (EXPIRY rev. 5,
                    Belegkette: anime-manga/funde-quellen.md Sweep 20.11.;
                    Semantik-Heimat: manga-sammlung/abo-katalog.md):
                    part.expiration == volume.publishing ist das
                    DIGITAL-RELEASE-Datum (API-verifiziert), NICHT das
                    Streaming-Ablaufdatum. Die 16.11.-Regel "Pre-Pub-Parts
                    laufen exakt MIT dem E-Book-Release ab" ("Ablauf ==
                    publishing") ist damit UEBERHOLT; reales Streaming-
                    Expiry berechnet die JNC-Website clientseitig per
                    Mid-Month-Regel: 15. des Folgemonats bei publishing
                    ab dem 9., sonst 15. desselben Monats, jeweils 10 Uhr;
                    Sonntag -> 16., Samstag -> 17. Belegt an Elegance in
                    Exile Bd. 1 / Middle-Aged and Mid No More Bd. 1
                    (publishing 23.11.2026 -> Expires 15.12.2026).
                    Pro akt PP-Band wird das berechnete Streaming-Expiry
                    als zusaetzliches Feld "prepub_expiry" ausgegeben.
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
[{number, publishing (Digital-Release), prepub_expiry (Streaming-Ablauf
per Mid-Month-Regel), readerStreamingEnabled}] - Grundlage fuer den
Anschluss-Join (scripts/jnc_anschluss.py x abo_snapshots/own_volumes.json).
Laeuft im Abo-Katalog-Check-Workflow als eigener Step nach den
Katalog-Lanes (continue-on-error: true).
Bewusst reine stdlib (urllib+json) - keine requests noetig.
"""
import json
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(os.environ.get("REPO_ROOT", str(Path.cwd())))
SNAP_DIR = REPO / "abo_snapshots"
SNAP_DIR.mkdir(exist_ok=True)
SNAP_PATH = SNAP_DIR / "jnc_availability.json"
CHANGES_PATH = REPO / "abo_changes.json"


def mid_month_expiry(pub):
    """JNC-Streaming-Expiry per Mid-Month-Regel (FINAL rev. 5, 20.11.2026).

    publishing (== part.expiration == Digital-Release) ist NICHT das
    Streaming-Expiry; Ziel-Datum = 15. des Folgemonats, wenn publishing
    auf den 9. oder spaeter faellt, sonst 15. desselben Monats (jeweils
    10 Uhr); Sonntag -> 16., Samstag -> 17. Formel aus den JNC-Website-
    Bundles extrahiert; Belege: Elegance in Exile Bd. 1, Middle-Aged and
    Mid No More Bd. 1 (publishing 23.11.2026 -> Expires 15.12.2026).
    Belegkette: anime-manga/funde-quellen.md Sweep 20.11.
    """
    y, m = pub.year, pub.month
    if pub.day >= 9:
        m += 1
        if m > 12:
            m, y = 1, y + 1
    exp = date(y, m, 15)
    wd = exp.weekday()          # Mo=0 ... So=6
    if wd == 6:                 # Sonntag -> 16.
        exp = exp + timedelta(days=1)
    elif wd == 5:               # Samstag -> 17.
        exp = exp + timedelta(days=2)
    return exp
API = "https://labs.j-novel.club/app/v2"


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
            if pub_dt > now:   # PP aktiv = publishing Zukunft (Digital-Release);
                               # Streaming-Ablauf per Mid-Month-Regel (rev. 5, 20.11.)
                prepub.append({
                    "number": v.get("number"),
                    "title": v.get("title", ""),
                    "publishing": pub,
                    "prepub_expiry": mid_month_expiry(
                        pub_dt.date()).isoformat(),
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

