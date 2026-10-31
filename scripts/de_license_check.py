#!/usr/bin/env python3
"""DE/EN-Lizenz-Fruehindikator ueber die MangaUpdates-API.

Prueft MAL-Titel aus dem MAL-Export gegen MangaUpdates (MU) und protokolliert
die Publisher je Serie in einen Snapshot. Ziel: Fruehindikator fuer neue
Nicht-JP/EN-Lizenzen (v. a. deutschsprachige Verlage) ohne manuelle Websuche.

WICHTIG (Methodik-Beleg 31.10.2026): MU listet Lizenzen LUECKENHAFT –
dt. Verlage fehlen dort teils komplett (Gegenbeleg: Kaoru Hana wa Rin to Saku,
dt. "Kaoru und Rin" bei Egmont, bei MU nicht gelistet). Ein leeres bzw.
unveraendertes Publisher-Feld ist KEIN Beweis fuer "keine dt. Lizenz";
der Newsletter-Check und der manuelle Monats-Check bleiben massgeblich.

Rotationsprinzip (Export 31.10.2026: 1301 aktive Titel = plan_to_read 989,
reading 307, on_hold 5): ein Voll-Sweep dauert bei MU-Rate ~60-70 Min; mit
--max-age 28 und Wochentakt prueft jeder Lauf nur das aelteste Viertel (~15 Min).

Check-Menge (dynamisch, NICHT statisch): Titel aus dem MAL-Export nach
--statuses (Default: plan_to_read,reading,on_hold = alles Aktive),
optional eingeschraenkt auf die IDs
aus --open-titles (offene Titel) und/oder erweitert um --ids.

Nur Python-stdlib, keine Third-Party-Abhaengigkeiten (wie alle Repo-Skripte).

Aufruf-Beispiele:
  python3 scripts/de_license_check.py                       # PTR-Sweep, deltas ausgeben
  python3 scripts/de_license_check.py --limit 5             # Testlauf
  python3 scripts/de_license_check.py --statuses plan_to_read   # nur PTR
  python3 scripts/de_license_check.py --ids 138373,157778   # gezielte IDs
Ausgaben: Snapshot abo_snapshots/de_licenses.json + Delta-Report auf stdout.
Exit-Code 0 immer (Info-Tool, kein CI-Gate).
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

API = "https://api.mangaupdates.com/v1"
UA = "sebrem-mal-export-repo de_license_check/1.0"

# Bekannte deutschsprachige Verlage (Normalform; Teilstring-Match, case-insensitive)
DE_PUBLISHERS = [
    "tokyopop", "carlsen", "egmont", "kaze", "crunchyroll manga", "altraverse",
    "panini", "dokico", "dani books", "hayabusa", "yomeru", "reprodukt",
    "splitter", "cross cult", "egmont manga",
]


def http_json(url, data=None, tries=3, timeout=30):
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={"User-Agent": UA, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            if attempt == tries:
                print(f"    WARN: HTTP-Fehler bei {url}: {e}", file=sys.stderr)
                return None
            time.sleep(3 * attempt)
    return None


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def mu_lookup(title):
    """Serie auf MU suchen; bestmöglichen Treffer zurueckgeben (id, title)."""
    payload = json.dumps({"search": title, "perpage": 5}).encode()
    res = http_json(API + "/series/search", data=payload)
    results = (res or {}).get("results") or []
    if not results:
        return None, None
    best = None
    n_title = norm(title)
    for r in results:
        rec = r.get("record") or {}
        rid, rtitle = rec.get("series_id"), rec.get("title") or ""
        if rid is None:
            continue
        if n_title and (n_title in norm(rtitle) or norm(rtitle) in n_title):
            best = (rid, rtitle)
            break
    if best is None:
        rec = results[0].get("record") or {}
        best = (rec.get("series_id"), rec.get("title") or "")
    return best


def mu_publishers(series_id):
    det = http_json(f"{API}/series/{series_id}")
    if not det:
        return None
    pubs = det.get("publishers") or []
    return [
        {
            "name": p.get("publisher_name") or p.get("name") or "",
            "type": p.get("type") or "",
            "notes": p.get("notes") or "",
        }
        for p in pubs
    ], det.get("status"), det.get("licensed")


def is_de(publisher_name, pub_type=""):
    """DE-Verlag nur wenn Name passt UND der Typ nicht eindeutig EN/JP ist
    (Tokyopop/Kodansha USA fuehren auch EN-Ausgaben unter dem VerlagNamen;
    Gegenbeleg-Falle 31.10.2026: 'TokyoPop [English]' != dt. Print)."""
    low = (publisher_name or "").lower()
    if not any(v in low for v in DE_PUBLISHERS):
        return False
    return (pub_type or "").strip().lower() not in ("original", "english")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--export", default="mal_manga.json", help="Pfad zum MAL-Export")
    ap.add_argument("--open-titles", default="open_titles.json",
                    help="Optional: nur IDs aus dieser Datei pruefen (leer/0 = ignorieren)")
    ap.add_argument("--snapshot", default="abo_snapshots/de_licenses.json")
    ap.add_argument("--statuses", default="plan_to_read,reading,on_hold",
                    help="Komma-Liste der MAL-Statuswerte; Default = alles Aktive "
                         "(plan_to_read,reading,on_hold) – nicht dropped/completed, "
                         "die brauchen keine Lizenz-Beobachtung mehr")
    ap.add_argument("--ids", default="", help="Zusaetzliche MAL-IDs, Komma-Liste")
    ap.add_argument("--limit", type=int, default=0, help="Max. Titel pro Lauf (0 = alle)")
    ap.add_argument("--max-age", type=int, default=28,
                    help="Titel mit frischem last_checked < N Tage ueberspringen. "
                         "Default 28 + Wochentakt = Rotationscheck: bei ~1300 aktiven "
                         "Titeln prueft jeder Lauf nur ~1/4 (~15 Min Laufzeit)")
    ap.add_argument("--sleep", type=float, default=1.5, help="Pause zwischen MU-Requests")
    args = ap.parse_args()

    with open(args.export, encoding="utf-8") as f:
        export = json.load(f)
    statuses = {s.strip() for s in args.statuses.split(",") if s.strip()}
    wanted_ids = {int(x) for x in args.ids.split(",") if x.strip().isdigit()}

    if os.path.exists(args.open_titles):
        try:
            with open(args.open_titles, encoding="utf-8") as f:
                ot = json.load(f)
            ot_ids = {int(k) for k in (ot or {}).keys() if str(k).isdigit()}
            if ot_ids:
                wanted_ids |= ot_ids  # offene Titel immer mitpruefen
        except (ValueError, OSError):
            pass  # fehlend/unlesbar -> keine Einschraenkung

    # Snapshot laden (inkrementell)
    snap = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "titles": {}}
    if os.path.exists(args.snapshot):
        with open(args.snapshot, encoding="utf-8") as f:
            old = json.load(f)
        snap["titles"] = old.get("titles") or {}

    # Check-Menge bestimmen
    todo = []
    for e in export.get("list", []):
        mid, ls = e.get("id"), e.get("list_status") or {}
        if mid in wanted_ids or ls.get("status") in statuses:
            todo.append((mid, e.get("title"), ls.get("status")))
    todo.sort(key=lambda x: x[0])
    if args.limit:
        todo = todo[: args.limit]

    now = dt.datetime.now(dt.timezone.utc)
    checked = unresolved = 0
    deltas = []
    for mid, title, status in todo:
        prev = snap["titles"].get(str(mid)) or {}
        lc = prev.get("last_checked") or ""
        if lc and prev.get("mu_series_id") and args.max_age > 0:
            try:
                age = (now - dt.datetime.fromisoformat(lc)).days
                if age < args.max_age:
                    continue
            except ValueError:
                pass
        print(f"  [{status}] {mid} {title[:60]}", file=sys.stderr)
        sid, mu_title = mu_lookup(title)
        time.sleep(args.sleep)
        if not sid:
            prev["unresolved"] = True
            prev["last_checked"] = now.isoformat()
            prev.setdefault("title", title)
            prev.setdefault("status", status)
            snap["titles"][str(mid)] = prev
            unresolved += 1
            continue
        pubs, mu_status, licensed = mu_publishers(sid)
        time.sleep(args.sleep)
        if pubs is None:
            unresolved += 1
            continue
        prev_pubs = {(p.get("name"), p.get("type")) for p in (prev.get("publishers") or [])}
        new_pubs = [p for p in pubs if (p.get("name"), p.get("type")) not in prev_pubs]
        entry = {
            "title": title, "status": status, "mu_title": mu_title,
            "mu_series_id": sid, "mu_status": mu_status, "mu_licensed": licensed,
            "publishers": pubs, "last_checked": now.isoformat(),
        }
        snap["titles"][str(mid)] = entry
        checked += 1
        if new_pubs:
            de_new = [p for p in new_pubs if is_de(p.get("name"), p.get("type"))]
            other_new = [p for p in new_pubs if not is_de(p.get("name"), p.get("type"))]
            deltas.append((mid, title, de_new, other_new))

    os.makedirs(os.path.dirname(args.snapshot), exist_ok=True)
    with open(args.snapshot, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)

    # Delta-Report
    print(f"de_license_check: {checked} geprüft, {unresolved} unaufgelöst, "
          f"{len(deltas)} Titel mit neuen Publishern.")
    for mid, title, de_new, other_new in deltas:
        for p in de_new:
            print(f"  !! DE-VERLAG: {title[:60]} (MAL {mid}) -> {p['name']} [{p['type']}] {p['notes']}")
        for p in other_new:
            print(f"  +  {title[:60]} (MAL {mid}) -> {p['name']} [{p['type']}] {p['notes']}")
    if deltas:
        print("Hinweis: MU listet DE-Verlage lückenhaft (s. Methodik im Skriptkopf) – "
              "Befunde im Monats-Check gegen Verlags-Primärquellen verifizieren.")
    return 0


if __name__ == "__main__":
    sys.exit(main())