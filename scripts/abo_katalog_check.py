#!/usr/bin/env python3
"""
Abo-Katalog-Check: MangaPlaza Read-in-Full + J-Novel Club (labs GraphQL).
Liefert abo_changes.json mit Neu-/Entfern-/Änderungs-Deltas gegen den
Snapshot in abo_snapshots/. Lane-Bewertung passiert NICHT hier, sondern in
Vibes wöchentlicher Abgleich-Aufgabe (siehe Abschnitt 4).
"""
import json, os, re, sys, datetime
from pathlib import Path
import requests
from bs4 import BeautifulSoup

# Repo-Root robust ermitteln: vom Workflow gesetzt (github.workspace),
# Fallback: aktuelles Verzeichnis (in Actions = Checkout-Root)
REPO = Path(os.environ.get("REPO_ROOT", str(Path.cwd())))
SNAP_DIR = REPO / "abo_snapshots"
SNAP_DIR.mkdir(exist_ok=True)
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ---------- MangaPlaza Read-in-Full ----------
MP_COOKIES = {"age_verification": "1", "adult": "1", "mp_age_check": "1"}

def scrape_mangaplaza():
    """Titel der Read-in-Full-Premium-Liste, alle Seiten bis Listenende."""
    titles = {}
    errors = []
    debug_head = ""
    for page in range(1, 21):          # Sicherheitslimit; real heute 9 Seiten
        url = ("https://mangaplaza.com/subscription/read_in_full/all_titles/"
               f"?page={page}")
        try:
            r = requests.get(url, headers=UA, cookies=MP_COOKIES, timeout=60)
            r.raise_for_status()
        except Exception as e:
            errors.append(f"mangaplaza page {page}: {e}")
            break
        text = BeautifulSoup(r.text, "html.parser").get_text("\n")
        lines = [l.strip() for l in text.split("\n")]
        if page == 1:
            debug_head = " ".join(lines)[:600]   # Diagnose, falls Parsing leer
        # Titel ist die Zeile direkt vor einer "Chapter X+ ..." Zeile
        found = 0
        for i, line in enumerate(lines):
            m = re.match(r"^Chapter (\d+)\+ (Complete|Ongoing) #(\d+)-(\d+)", line)
            if not m:
                continue
            title = lines[i - 1]
            if not title or title.lower() in ("find titles", "all genres"):
                continue
            author = lines[i + 2] if i + 2 < len(lines) else ""
            rating = ""
            for j in range(i, min(i + 8, len(lines))):
                if re.match(r"^\d\.\d \(\d[\d,]*\)$", lines[j]):
                    rating = lines[j]
                    break
            titles[title] = {
                "rating": rating,
                "chapters": line.replace("Chapter ", ""),
                "author": author,
                "url": url,
            }
            found += 1
        if found == 0:
            if page == 1:
                errors.append(
                    "mangaplaza: 0 Titel auf Seite 1 – HTML-Anfang: "
                    + debug_head)
            break                       # Listenende erreicht
    return titles, errors

# ---------- J-Novel Club + JNC Nina (labs REST API v2) ----------
# Endpunkt-Schema aus jncep (github.com/gvellut/jncep, jncapi.py):
#   GET {base}/app/v2/series?format=json&limit=500&skip=N
#   Pagination: response.pagination {limit, lastPage}
# Auth: optional; falls Secrets JNC_EMAIL/JNC_PASSWORD gesetzt sind,
# wird Bearer-Token per POST /app/v2/auth/login geholt.

def _jnc_login(base, email, password):
    r = requests.post(
        f"{base}/app/v2/auth/login?format=json",
        json={"login": email, "password": password, "slim": True},
        headers=UA, timeout=60)
    r.raise_for_status()
    return r.json()["id"]

def scrape_jnc():
    """Serienliste inkl. Tags/Status; JNC main + JNC Nina (falls konfiguriert)."""
    sources = [("jnc", "https://labs.j-novel.club")]
    nina_user = os.environ.get("JNC_NINA_EMAIL")
    nina_pass = os.environ.get("JNC_NINA_PASSWORD")
    if nina_user and nina_pass:
        sources.append(("jnc_nina", "https://api.jnc-nina.eu"))
    all_out, all_err = {}, []
    for key, base in sources:
        out, errors = {}, []
        headers = dict(UA)
        try:
            user = os.environ.get("JNC_EMAIL")
            pw = os.environ.get("JNC_PASSWORD")
            if key == "jnc_nina":
                user, pw = nina_user, nina_pass
            if user and pw:
                token = _jnc_login(base, user, pw)
                headers["Authorization"] = f"Bearer {token}"
            skip = 0
            while True:
                r = requests.get(
                    f"{base}/app/v2/series",
                    params={"format": "json", "limit": 500, "skip": skip},
                    headers=headers, timeout=60)
                if r.status_code == 401:
                    errors.append(f"{key}: 401 – Auth nötig/ungültig "
                                  "(Secrets prüfen)")
                    break
                r.raise_for_status()
                data = r.json()
                series = data.get("series", [])
                for s in series:
                    out[s.get("title", "?")] = {
                        "slug": s.get("slug"),
                        "status": s.get("status"),
                        "type": s.get("type"),
                        "tags": sorted(t.get("name", "") if isinstance(t, dict)
                                       else str(t)
                                       for t in (s.get("tags") or [])),
                        "imprint": (s.get("imprint") or {}).get("name")
                                   if isinstance(s.get("imprint"), dict)
                                   else s.get("imprint"),
                    }
                pagination = data.get("pagination", {})
                if pagination.get("lastPage") or not series:
                    break
                skip = int(pagination.get("limit", 500)) + skip
        except Exception as e:
            errors.append(f"{key}: {e}")
        # Namenskollisionen zwischen JNC und Nina vermeiden
        for t, v in out.items():
            all_out[f"[{key}] {t}"] = v
        all_err.extend(errors)
    return all_out, all_err

# ---------- Diff ----------
def diff(name, current, snapshot_path):
    old = {}
    if snapshot_path.exists():
        old = json.loads(snapshot_path.read_text(encoding="utf-8"))
    new = [t for t in current if t not in old]
    removed = [t for t in old if t not in current]
    changed = [t for t in current
               if t in old and old[t] != current[t]]
    snapshot_path.write_text(
        json.dumps(current, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8")
    return {"new": {t: current[t] for t in sorted(new)},
            "removed": sorted(removed),
            "changed": {t: {"old": old[t], "new": current[t]}
                         for t in sorted(changed)}}

def main():
    changes = {"run_at": now(), "mangaplaza": {}, "jnc": {}, "errors": []}

    mp, err = scrape_mangaplaza()
    changes["errors"] += err
    if mp:
        changes["mangaplaza"] = diff(
            "mangaplaza", mp, SNAP_DIR / "mangaplaza_readinfull.json")

    jnc, err = scrape_jnc()
    changes["errors"] += err
    if jnc:
        changes["jnc"] = diff("jnc", jnc, SNAP_DIR / "jnc_series.json")

    (REPO / "abo_changes.json").write_text(
        json.dumps(changes, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Repo-Root: {REPO}")
    print(f"Titel MangaPlaza: {len(mp)} · JNC: {len(jnc)}")
    print(json.dumps(changes, ensure_ascii=False, indent=1)[:2000])

if __name__ == "__main__":
    sys.exit(main())
