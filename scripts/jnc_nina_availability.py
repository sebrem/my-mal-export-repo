#!/usr/bin/env python3
"""
JNC-Nina-Verfuegbarkeits-Lane (Stand 17.11.2026, erste Iteration).

Erweitert die JNC-Pre-Pub-Logik (jnc_availability.py) auf JNC Nina
(deutschsprachige JNC-Schwester, jnc-nina.eu). Sebastian ist Nina-
Jahres-Mitglied (60 EUR/Jahr) – die Mitgliedschaft gewährt die
woechentlichen Vorab-Teile (Pre-Pub) ALLER laufenden Nina-Serien,
analog der Pre-Pub-Seite des JNC-USA-Dual-Pass. Annahme (analog
JNC USA, 16.11. API-verifiziert dort): Part-Ablauf == Volume-
"publishing" == E-Book-Release. NOCH NICHT API-verifiziert fuer
Nina – erster Produktivlauf validiert die Feldsemantik.

Unterschied zu JNC USA: Nina hat KEINE Backlist-Library im Abo
(kein readerStreamingEnabled-Aequivalent erwartet); "library"-Feld
wird trotzdem erhoben, falls die API ein Stream-/Loan-Flag je Band
liefert (Nina-Loan/Geliehene = membership-abhaengig).

API-Route: anders als labs.j-novel.club ist die Nina-API-Route
NOCH NICHT verifiziert – der Mistral-Proxy wird von api.jnc-nina.eu
geblockt (kein Beleg gegen die GitHub-Runner-Route, s. Feed-Block-
Regel). Das Skript probiert Kandidaten-Basen durch:
  1. https://api.jnc-nina.eu/app/v2   (JNC-Stack-Vermutung)
  2. https://api.jnc-nina.eu/api/v2
  3. https://api.jnc-nina.eu/api
  4. https://jnc-nina.eu/api/v2
je mit leerem User-Agent (blank-UA-Pflicht, analog JNC USA) und
alternativ Browser-UA als Fallback. Die funktionierende Basis wird
im Snapshot als "_api_base" dokumentiert; schlaegt alles fehl,
endet der Step mit Diagnose-Ausgabe (continue-on-error im Workflow)
und veraendert keinen bestehenden Snapshot.

Snapshots: abo_snapshots/jnc_nina_availability.json
Diff (new/removed/changed): Merge nach abo_changes.json["jnc_nina"].
Bandgenau (Einheiten-Regel: JNC-Einheit = VOLUME).
Bewusst reine stdlib (urllib+json).
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(os.environ.get("REPO_ROOT", str(Path.cwd())))
SNAP_DIR = REPO / "abo_snapshots"
SNAP_DIR.mkdir(exist_ok=True)
SNAP_PATH = SNAP_DIR / "jnc_nina_availability.json"
CHANGES_PATH = REPO / "abo_changes.json"

# Kandidaten-Basen in Verifikations-Reihenfolge
API_BASE_CANDIDATES = [
    "https://api.jnc-nina.eu/app/v2",
    "https://api.jnc-nina.eu/api/v2",
    "https://api.jnc-nina.eu/api",
    "https://jnc-nina.eu/api/v2",
]
UA_CANDIDATES = ["", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"]


def api_get(url, ua):
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def probe_api():
    """Findet eine funktionierende API-Basis; None wenn alle scheitern."""
    for base in API_BASE_CANDIDATES:
        for ua in UA_CANDIDATES:
            try:
                batch = api_get(f"{base}/series?format=json&limit=1", ua)
                items = batch.get("series", batch if isinstance(batch, list) else [])
                if items:
                    print(f"jnc_nina: API-Basis {base} OK "
                          f"(UA={'leer' if ua == '' else 'Browser'})")
                    return base, ua
            except Exception:
                continue
    return None, None


def fetch_all_series(base, ua):
    series, skip = [], 0
    while True:
        batch = api_get(f"{base}/series?format=json&limit=500&skip={skip}", ua)
        items = batch.get("series", batch if isinstance(batch, list) else [])
        if not items:
            break
        series.extend(items)
        if len(items) < 500:
            break
        skip += 500
    return series


def fetch_volumes(base, ua, slug):
    volumes, skip = [], 0
    while True:
        batch = api_get(
            f"{base}/series/{slug}/volumes?format=json&limit=500&skip={skip}", ua)
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
    base, ua = probe_api()
    if not base:
        print("jnc_nina_availability: KEINE der Kandidaten-API-Basen "
              "erreichbar. Diagnose: (1) Route manuell im Browser "
              "pruefen (DevTools/Netzwerk-Tab auf jnc-nina.eu Serien-"
              "seite), (2) gefundene Basis hier in API_BASE_CANDIDATES "
              "eintragen, (3) ggf. Auth-Secrets JNC_NINA_EMAIL/"
              "JNC_NINA_PASSWORD anschliessen. Kein Snapshot veraendert.")
        return 1

    series_list = fetch_all_series(base, ua)
    if not series_list:
        print("jnc_nina_availability: 0 Serien erkannt - API pruefen")
        return 1

    current = {}
    for i, s in enumerate(series_list, 1):
        slug = s.get("slug") or s.get("id")
        if not slug:
            continue
        try:
            vols = fetch_volumes(base, ua, slug)
        except Exception as e:
            print(f"  [{i}/{len(series_list)}] {slug}: Volume-Fetch "
                  f"fehlgeschlagen ({e}) - Serie uebersprungen")
            continue
        # Nina-Loan/Stream-Flag: Feldname unbekannt - defensive Erhebung
        # aller boolean Flaggen, die "stream" enthalten (falls vorhanden).
        library = any(
            v.get(k) for v in vols
            for k in v if isinstance(v.get(k), bool) and "stream" in k.lower()
        )
        prepub = []
        for v in vols:
            pub = v.get("publishing")
            if not pub:
                continue
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except ValueError:
                continue
            if pub_dt > now:   # PP aktiv = publishing Zukunft (Annahme analog JNC USA)
                prepub.append({
                    "number": v.get("number"),
                    "title": v.get("title", ""),
                    "publishing": pub,
                })
        lib_nums = sorted(
            n for n in (v.get("number") for v in vols
                        if any(k in v and v.get(k) for k in v
                               if isinstance(v.get(k), bool) and "stream" in k.lower()))
            if n is not None)
        current[slug] = {
            "title": s.get("title", ""),
            "status": s.get("status", ""),
            "library": library,
            "library_vols": lib_nums,
            "prepub_active": prepub,
        }
        if i % 25 == 0:
            print(f"  ... {i}/{len(series_list)} Nina-Serien verarbeitet")

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
    snap = {"_api_base": base, "_generated_at":
            now.isoformat(timespec="seconds"), **current}
    SNAP_PATH.write_text(
        json.dumps(snap, ensure_ascii=False, indent=1, sort_keys=True),
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
    changes["jnc_nina"] = delta
    CHANGES_PATH.write_text(
        json.dumps(changes, ensure_ascii=False, indent=1),
        encoding="utf-8")

    n_pub = sum(1 for v in current.values() if v["prepub_active"])
    print(f"JNC-Nina-Verfuegbarkeit: {len(current)} Serien "
          f"(Pre-Pub aktiv: {n_pub}; neu: {len(new_keys)}, "
          f"entfernt: {len(removed)}, geaendert: {len(changed)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
