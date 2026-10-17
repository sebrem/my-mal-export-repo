#!/usr/bin/env python3
"""
Abo-Katalog-Check: MangaPlaza Read-in-Full + J-Novel Club (labs GraphQL).
Liefert abo_changes.json mit Neu-/Entfern-/Änderungs-Deltas gegen den
Snapshot in abo_snapshots/. Lane-Bewertung passiert NICHT hier, sondern in
Vibes wöchentlicher Abgleich-Aufgabe (siehe Abschnitt 4).
"""
import json, re, sys, datetime
from pathlib import Path
import requests
from bs4 import BeautifulSoup

REPO = Path(__file__).resolve().parents[1]
SNAP_DIR = REPO / "abo_snapshots"
SNAP_DIR.mkdir(exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AboKatalogCheck/1.0"}

def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ---------- MangaPlaza Read-in-Full ----------
def scrape_mangaplaza():
    """Titel der Read-in-Full-Premium-Liste, alle Seiten bis Listenende."""
    titles = {}
    errors = []
    for page in range(1, 21):          # Sicherheitslimit; real heute 9 Seiten
        url = ("https://mangaplaza.com/subscription/read_in_full/all_titles/"
               f"?page={page}")
        try:
            r = requests.get(url, headers=UA, timeout=60)
            r.raise_for_status()
        except Exception as e:
            errors.append(f"mangaplaza page {page}: {e}")
            break
        text = BeautifulSoup(r.text, "html.parser").get_text("\n")
        lines = [l.strip() for l in text.split("\n")]
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
            break                       # Listenende erreicht
    return titles, errors

# ---------- J-Novel Club (labs GraphQL) ----------
JNC_QUERY = """
query {
  seriesPagination {
    items {
      title
      slug
      status
      tags { name }
      imprint { name }
    }
  }
}
"""

def scrape_jnc():
    """Serienliste inkl. Tags/Status über die offizielle labs-API."""
    try:
        r = requests.post(
            "https://labs.j-novel.club/graphql",
            json={"query": JNC_QUERY},
            headers={**UA, "Content-Type": "application/json"},
            timeout=60,
        )
        data = r.json()
        if "errors" in data or "data" not in data or not data.get("data"):
            return {}, [{"jnc": f"GraphQL-Fehler: {json.dumps(data)[:300]}"}]
        items = data["data"].get("seriesPagination", {}).get("items", [])
        out = {}
        for s in items:
            out[s["title"]] = {
                "slug": s.get("slug"),
                "status": s.get("status"),
                "tags": sorted(t.get("name", "") for t in (s.get("tags") or [])),
                "imprint": (s.get("imprint") or {}).get("name"),
            }
        return out, []
    except Exception as e:
        return {}, [{"jnc": f"Verbindung/Schema-Fehler: {e}"}]

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
