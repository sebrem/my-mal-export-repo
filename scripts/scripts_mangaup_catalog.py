#!/usr/bin/env python3
"""
Manga-UP!-Global-Katalog-Lane (Stand 17.10.2026, erste Iteration).
Scrapt den oeffentlichen Katalog von global.manga-up.com/search
(serverseitig komplett gerendert, ~456 Titel inkl. W-Z; Query-Parameter
filtern serverseitig NICHT). KEIN Abo: Verfuegbarkeits-/Plattform-Lane
fuer Sebastians kapitelweise Manga-UP!-Lektuere.
Snapshots: abo_snapshots/mangaup_catalog.json
Diff (new/removed/changed): Merge nach abo_changes.json["mangaup"].
Laeuft im Abo-Katalog-Check-Workflow NACH abo_katalog_check.py.
Anker: <a href="/manga/<id>"> mit Namens-Div "appTitle__name"; die
Badges (UP/NEW/Subscribed) darin werden vor der Titel-Extraktion
entfernt. Fallback: Cover-Alt-Text ("Cover of <Titel>").
Bewusst reine stdlib (urllib+re) - keine requests/bs4 noetig.
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

REPO = Path(os.environ.get("REPO_ROOT", str(Path.cwd())))
SNAP_DIR = REPO / "abo_snapshots"
SNAP_DIR.mkdir(exist_ok=True)
SNAP_PATH = SNAP_DIR / "mangaup_catalog.json"
CHANGES_PATH = REPO / "abo_changes.json"
URL = "https://global.manga-up.com/search"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")


def scrape_mangaup():
    """Gibt {titel: {title_id, title_url}} zurueck."""
    req = urllib.request.Request(URL, headers={
        "User-Agent": UA,
        "Accept": "text/html,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    titles = {}
    for card in re.finditer(r'<a href="/manga/(\d+)">', html):
        mid = card.group(1)
        if mid in titles:
            continue
        # Namens-Div der Karte (bis zum Karten-Ende): Badges entfernen,
        # dann Tags strippen -> Titel-Text.
        seg = html[card.start():card.start() + 3000]
        m = re.search(r'appTitle__name__[A-Za-z0-9]+">(.*?)</div></div>',
                      seg, re.S)
        title = ""
        if m:
            inner = re.sub(r'<(p|span) class="Badge[^>]*>.*?</\1>', '',
                           m.group(1), flags=re.S)
            title = " ".join(re.sub(r"<[^>]+>", "", inner).split())
        if not title:                 # Fallback: Cover-Alt-Text
            alt = re.search(r'<img alt="Cover of ([^"]+)"', seg)
            if alt:
                title = alt.group(1).strip()
        if title and title not in titles:
            titles[title] = {
                "title_id": mid,
                "title_url": f"https://global.manga-up.com/manga/{mid}",
            }
    return titles


def diff(current):
    old = {}
    if SNAP_PATH.exists():
        try:
            old = json.loads(SNAP_PATH.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    new = sorted(t for t in current if t not in old)
    removed = sorted(t for t in old if t not in current)
    changed = sorted(t for t in current
                     if t in old and old[t] != current[t])
    SNAP_PATH.write_text(
        json.dumps(current, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8")
    return {
        "new": {t: current[t] for t in new},
        "removed": removed,
        "changed": {t: {"old": old[t], "new": current[t]}
                    for t in changed},
    }


def main():
    current = scrape_mangaup()
    if not current:
        print("mangaup: 0 Titel erkannt - Parser pruefen "
              "(Seite geaendert?)")
        return 1
    delta = diff(current)

    changes = {}
    if CHANGES_PATH.exists():
        try:
            changes = json.loads(CHANGES_PATH.read_text(encoding="utf-8"))
        except Exception:
            changes = {}
    changes["mangaup"] = delta
    CHANGES_PATH.write_text(
        json.dumps(changes, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"Titel Manga UP!: {len(current)} "
          f"(neu: {len(delta['new'])}, entfernt: {len(delta['removed'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
