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
    for page in range(1, 51):          # Sicherheitslimit; echte Grenze via
                                       # 'keine neuen Titel' (Seiten > letzte
                                       # liefern wiederholt Inhalt, kein 404)
        url = ("https://mangaplaza.com/subscription/read_in_full/all_titles/"
               f"?page={page}")
        try:
            r = session.get(url, timeout=60)
            r.raise_for_status()
        except Exception as e:
            errors.append(f"mangaplaza page {page}: {e}")
