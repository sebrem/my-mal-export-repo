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
MP_COOKIES = {"age_verification": "1", "adult": "1", "mp_cookie_notice": "OK"}

def _mp_login(session):
    """Login per /api/login/check/ (Laravel-Session, CSRF aus Cookie).
    Benötigt MP_EMAIL/MP_PASSWORD als Repo-Secrets. Gibt True zurück,
    wenn der Login erfolgreich war."""
    email = os.environ.get("MP_EMAIL")
    password = os.environ.get("MP_PASSWORD")
    if not email or not password:
        return False, ("mangaplaza: keine Credentials (MP_EMAIL/"
                       "MP_PASSWORD) – Read-in-Full ist nur eingeloggt "
                       "sichtbar, Schritt wird übersprungen")
    # 1. CSRF-Token über Login-Seite + Cookie beziehen
    r = session.get("https://mangaplaza.com/login/", timeout=60)
    r.raise_for_status()
    csrf = session.cookies.get("XSRF-TOKEN", "")
    if not csrf:
        return False, "mangaplaza: kein XSRF-TOKEN erhalten"
    # 2. Login-POST (Felder: email, password; URL-encoded wie im Browser)
    r = session.post(
        "https://mangaplaza.com/api/login/check/",
        data={"email": email, "password": password},
        headers={
            "X-CSRF-TOKEN": csrf,
            "X-XSRF-TOKEN": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://mangaplaza.com/login/?back_url=%2F",
        },
        timeout=60,
    )
    try:
        body = r.json()
    except Exception:
        body = {}
    if r.status_code == 200 and body.get("result", {}).get("status") == "OK":
        return True, None
    return False, (f"mangaplaza: Login fehlgeschlagen (HTTP {r.status_code}, "
                   f"Antwort: {json.dumps(body)[:200]})")

def scrape_mangaplaza():
    """Titel der Read-in-Full-Premium-Liste, alle Seiten bis Listenende.
    Parser-Anchor: Jede Titel-Karte enthält einen Link auf /title/<id>/
    und die Kapitel-Info 'Chapter X+ (Complete|Ongoing) #N-M'."""
    titles = {}
    errors = []
    debug_head = ""
    session = requests.Session()
    session.headers.update(UA)
    session.cookies.update(MP_COOKIES)
    ok, err = _mp_login(session)
    if not ok:
        return {}, [err] if err else []
    chapter_re = re.compile(r"Chapter (\d+)\+ (Complete|Ongoing) #(\d+)-(\d+)")
    rating_re = re.compile(r"\d\.\d \(\d[\d,]*\)")
    for page in range(1, 21):          # Sicherheitslimit; real heute 9 Seiten
        url = ("https://mangaplaza.com/subscription/read_in_full/all_titles/"
               f"?page={page}")
        try:
            r = session.get(url, timeout=60)
            r.raise_for_status()
        except Exception as e:
            errors.append(f"mangaplaza page {page}: {e}")
            break
        soup = BeautifulSoup(r.text, "html.parser")
        if page == 1:
            debug_head = soup.get_text(" ", strip=True)[:600]
        # Kapitel-Info-Elemente sind der robuste Einstiegspunkt
        found = 0
        for el in soup.find_all(string=chapter_re):
            # Karten-Container suchen (bis 5 Ebenen hoch): derjenige,
            # der genau einen /title/<id>/-Link enthält
            card = el
            for _ in range(5):
                card = card.parent
                if card is None:
                    break
                links = [a for a in card.find_all("a", href=True)
                         if re.search(r"/title/\d+", a["href"])]
                if len(links) >= 1:
                    break
            else:
                card = None
            if card is None:
                continue
            link = links[0]
            m = chapter_re.search(el if isinstance(el, str) else el.get_text())
            title = link.get_text(" ", strip=True)
            if not title:
                continue
            card_text = card.get_text(" ", strip=True)
            rm = rating_re.search(card_text)
            titles[title] = {
                "rating": rm.group(0) if rm else "",
                "chapters": m.group(0).replace("Chapter ", "") if m else "",
                "title_url": "https://mangaplaza.com"
                             + re.sub(r"\?.*$", "", link["href"]),
                "list_url": url,
            }
            found += 1
        if found == 0:
            if page == 1:
                errors.append(
                    "mangaplaza: 0 Titel auf Seite 1 – Text-Anfang: "
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

# ---------- Stufe 2: JNC-Watchlist-Volume-Status ----------
def fetch_watchlist_status():
    """Für jede Serie in jnc_watchlist.json Volume-Daten holen
    (Band-Anzahl, letzter Release, Status). Liest die Watchlist NUR –
    Änderungen daran passieren per Copy-Paste/Chat (Lane-Entscheidung)."""
    wl_path = REPO / "jnc_watchlist.json"
    if not wl_path.exists():
        return {}, [{"watchlist": "jnc_watchlist.json fehlt – Stufe 2 übersprungen"}]
    watchlist = json.loads(wl_path.read_text(encoding="utf-8"))
    out, errors = {}, []
    headers = dict(UA)
    user = os.environ.get("JNC_EMAIL")
    pw = os.environ.get("JNC_PASSWORD")
    try:
        if user and pw:
            token = _jnc_login("https://labs.j-novel.club", user, pw)
            headers["Authorization"] = f"Bearer {token}"
        for entry in watchlist:
            key = entry.get("key")                      # "[jnc] Titel"
            slug = entry.get("slug")
            base = ("https://api.jnc-nina.eu" if key.startswith("[jnc_nina]")
                    else "https://labs.j-novel.club")
            try:
                r = requests.get(f"{base}/app/v2/series/{slug}",
                                params={"format": "json"},
                                headers=headers, timeout=60)
                r.raise_for_status()
                series = r.json().get("series", {})
                r2 = requests.get(f"{base}/app/v2/series/{slug}/volumes",
                                  params={"format": "json", "limit": 500},
                                  headers=headers, timeout=60)
                r2.raise_for_status()
                volumes = r2.json().get("volumes", [])
                released = [v for v in volumes
                            if (v.get("publishDate") or "") <= now()[:10]]
                out[key] = {
                    "slug": slug,
                    "status": series.get("status"),
                    "volumes_total": len(volumes),
                    "volumes_released": len(released),
                    "latest_volume": (released[-1].get("title")
                                      if released else None),
                    "latest_date": (released[-1].get("publishDate")
                                    if released else None),
                }
            except Exception as e:
                errors.append(f"watchlist {key}: {e}")
    except Exception as e:
        errors.append(f"watchlist login: {e}")
    return out, errors

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

    # Stufe 2: Watchlist-Volume-Status (Deltas gegen letzten Stand)
    wl_status, err = fetch_watchlist_status()
    changes["errors"] += err
    if wl_status:
        changes["jnc_watchlist"] = diff(
            "watchlist", wl_status, SNAP_DIR / "jnc_watchlist_status.json")

    (REPO / "abo_changes.json").write_text(
        json.dumps(changes, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Repo-Root: {REPO}")
    print(f"Titel MangaPlaza: {len(mp)} · JNC: {len(jnc)}")
    print(json.dumps(changes, ensure_ascii=False, indent=1)[:2000])

if __name__ == "__main__":
    sys.exit(main())
