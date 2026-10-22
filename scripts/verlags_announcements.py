#!/usr/bin/env python3
"""Verlags-Announcements-Harvester (stdlib only).

Liest wöchentlich Neuigkeiten-/Lizenz-Seiten der relevanten Verlage sowie
Manga-Passion als DE-Aggregator ab und schreibt:

  verlags_announcements.json         - kumulierter Stand, Retention RETENTION_DAYS
  verlags_announcements_changes.json - nur die NEUEN Items dieses Laufs

Design analog zu scripts/mangaup_catalog.py: keine Third-Party-Deps,
pro Quelle try/except (Fehler landen in sources.<name>.errors, nie ein
Harakiri-Exit), Dedup per URL, neu = URL nicht im bisherigen Stand.

Quellen-URLs sind bewusst zentral in SOURCES gepflegt: Bei Umbau einer
Verlagsseite nur dort fixen, nach dem ersten manuellen Lauf ggf. anhand
der errors-Liste korrigieren. RSS wird bevorzugt, HTML-Fallback generisch
(Anchor-Extraktion mit Titelfilter).
"""

import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.environ.get("REPO_ROOT", os.getcwd())
STATE_FILE = os.path.join(REPO_ROOT, "verlags_announcements.json")
CHANGES_FILE = os.path.join(REPO_ROOT, "verlags_announcements_changes.json")
RETENTION_DAYS = 180
TIMEOUT = 25
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) HeadlessChrome/126 Safari/537.36")

# Quelle -> Endpunkte. Typ "rss" (Atom/RSS, Titel+Link+Datum) oder
# "html" (Anchor-Extraktion). Mehrere Endpunkte je Quelle sind erlaubt;
# jeder wird einzeln gegen Fehler behandelt.
SOURCES = {
    "carlsen": [
        {"type": "html", "url": "https://www.carlsen.de/manga/monatsuebersicht"},
    ],
    "tokyopop": [
        {"type": "html", "url": "https://tokyopop.de/News/"},
    ],
    "manga2you-altraverse": [  # altraverse-Lizenzrunden (altraverse.de/news/ = 404, 22.10.2026)
        {"type": "html", "url": "https://www.manga2you.de/kategorie/verlage/altraverse/"},
    ],
    "dokico": [
        {"type": "html", "url": "https://dokico.de/"},
    ],
    "danibooks": [
        {"type": "html", "url": "https://www.danibooks.de/"},
    ],
    "jnc": [
        {"type": "rss", "url": "https://api.j-novel.club/rss"},  # gelegentlich kaputt (22.10.2026: not well-formed) - Fallback unten greift
        {"type": "html", "url": "https://j-novel.club/blog"},
    ],
    "yenpress": [
        {"type": "html", "url": "https://yenpress.com/news/category/news"},
        {"type": "html", "url": "https://yenpress.com/news/tag/announcements"},
    ],
    "manga-passion": [  # DE-Aggregator; /articles selbst ist JS-gerendert (nur 4 Links, 22.10.2026) - Unterseiten als Kandidaten, Fehler je Endpunkt toleriert
        {"type": "html", "url": "https://www.manga-passion.de/news"},
        {"type": "html", "url": "https://www.manga-passion.de/news/german"},
        {"type": "html", "url": "https://www.manga-passion.de/news/international"},
    ],
}

ANCHOR_RE = re.compile(
    r'<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")
TITLE_MIN = 15   # Noise-Filter: Nav-/Footer-Links sind meist kuerzer
TITLE_MAX = 220

# Noise-Filter (22.10.2026, nach Erst-Lauf-Analyse): Nav-/Footer-/Policy-/Shop-
# Service-Links rauswerfen, damit die Diffs sauber bleiben. URL- und Titel-
# Muster kombiniert; bewusst breit - falsch Positive verlieren nichts,
# weil die Titel im Changes-File ohnehin nochmal gelesen werden.
NOISE_URL_RE = re.compile(
    r"(/policies/|datenschutz|datenschutzerklaerung|/agb|widerruf|"
    r"barrierefreiheit|/faq|/tos\b|accessibility|/search\b|/suche\b|"
    r"genre|/account|/checkout|/cart\b|login|authentication|password|"
    r"controller=|/contact|/kontakt|shipping|versand-und|terms-of-service|"
    r"privacy|/help|/hilfe|/service/|/informationen/|stellenangebote|"
    r"apps\.shopify|edesigninteractive|/widgets/|/stoeberecke/|/katalog\b|"
    r"rights|lizenzen\b|unterrichtsmaterial|/kinderbuch/|/comics/|"
    r"/schuler|-uebersicht\b|/kalender\b|/merchandising|/leseproben\b)",
    re.IGNORECASE,
)
NOISE_TITLE_RE = re.compile(
    r"^(alle |zur kategorie|zeige alle|nach [\"'„]?[ÄA]hnlichem|"
    r"hier geht|persönlich|hauptadresse|rückvergütung|meine bestellungen|"
    r"passwort|vertrag|kostenloser|website by|search by|teardownbedingungen|"
    r"teilnahmebedingungen|versand und|kontaktinformationen)",
    re.IGNORECASE,
)


def is_noise(url, title):
    if NOISE_URL_RE.search(url):
        return True
    if NOISE_TITLE_RE.match(title):
        return True
    return False


def fetch(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Language": "de,en;q=0.8"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
        ctype = resp.headers.get("Content-Type", "")
    return raw, ctype


def clean(text):
    text = TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def harvest_rss(url):
    raw, _ = fetch(url)
    root = ET.fromstring(raw)
    items = []
    # RSS 2.0 (<item>) und Atom (<entry>) abdecken
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1].lower()
        if tag not in ("item", "entry"):
            continue
        title = link = date = None
        for child in node:
            ctag = child.tag.rsplit("}", 1)[-1].lower()
            if ctag == "title" and child.text:
                title = clean(child.text)
            elif ctag == "link":
                link = (child.get("href") or child.text or "").strip()
            elif ctag in ("pubdate", "published", "updated") and child.text:
                date = child.text.strip()[:10]
        if title and link:
            items.append({"title": title[:TITLE_MAX], "url": link, "date": date})
    return [it for it in items if not is_noise(it["url"], it["title"])]


def harvest_html(url):
    raw, ctype = fetch(url)
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype, re.IGNORECASE)
    if m:
        charset = m.group(1)
    html = raw.decode(charset, errors="replace")
    items, seen = [], set()
    for href, inner in ANCHOR_RE.findall(html):
        title = clean(inner)
        if not (TITLE_MIN <= len(title) <= TITLE_MAX):
            continue
        if not href.startswith("http"):
            href = href if href.startswith("/") else "/" + href
            href = re.match(r"(https?://[^/]+)", url).group(1) + href
        if href in seen:
            continue
        seen.add(href)
        if is_noise(href, title):
            continue
        items.append({"title": title, "url": href, "date": None})
    return items


def main():
    now = datetime.now(timezone.utc)
    state = {"generated_at": now.isoformat(), "sources": {}}
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            old = json.load(fh)
    except (OSError, ValueError):
        old = {"sources": {}}

    known_urls = {
        it["url"]
        for src in old.get("sources", {}).values()
        for it in src.get("items", [])
    }
    changes = {"generated_at": now.isoformat(), "new": {}}
    cutoff = (now - timedelta(days=RETENTION_DAYS)).isoformat()

    for name, endpoints in SOURCES.items():
        items, errors = [], []
        for ep in endpoints:
            try:
                got = harvest_rss(ep["url"]) if ep["type"] == "rss" else harvest_html(ep["url"])
                items.extend(got)
            except Exception as exc:  # pro Endpunkt tolerieren
                errors.append(f"{ep['url']}: {type(exc).__name__}: {exc}")
        # Dedup innerhalb der Quelle (zweiter Endpunkt oft Teilmenge)
        dedup, seen = [], set()
        for it in items:
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            dedup.append(it)
        state["sources"][name] = {"items": dedup, "errors": errors}
        fresh = [it for it in dedup if it["url"] not in known_urls]
        if fresh:
            changes["new"][name] = fresh

    # Retention: Item-Staende behalten; nur veraltete Datumseintraege droppen
    for src in state["sources"].values():
        src["items"] = [
            it for it in src["items"]
            if it.get("date") is None or it["date"] >= cutoff[:10]
        ]

    for path, payload in ((STATE_FILE, state), (CHANGES_FILE, changes)):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)

    n_new = sum(len(v) for v in changes["new"].values())
    n_err = sum(len(s["errors"]) for s in state["sources"].values())
    print(f"verlags_announcements: {n_new} neue Items, {n_err} Endpunkt-Fehler")
    # Fehler sind erwartbar (Seiten-Umbauten) und beenden den Lauf nicht;
    # nur Totalausfall (alle Quellen leer + Fehler) ist warnend.
    if n_err and all(not s["items"] for s in state["sources"].values()):
        print("WARNUNG: Alle Quellen fehlgeschlagen - URLs in SOURCES pruefen")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
