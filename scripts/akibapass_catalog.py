#!/usr/bin/env python3
"""
Akiba-Pass-Shop-Digitalprogramm-Lane (Stand 28.10.2026, erste Iteration).
Digitale Anime-Kauf-/Leihversionen aus dem Akiba Pass Shop
(https://akibapassshop.de/collections/digital). KEIN Abo: Kauf-/Leih-
Fallback-Lane gemaess Kernregel "Filme ohne Stream -> Kauf bei Akiba
Pass" (Streaming-Monitoring, Topic anime-manga).
Quelle: Shopify-products.json-Endpunkt der Collection (strukturiert,
kein HTML-Parsing; Pagination via limit/page, Stand ~154 Produkte).
Pro Produkt: Titel, Fassungen (OmU/DE) je Variante mit Preis und
Verfuegbarkeit, SKU (kodiert Fassung, z. B. 26d005-omu-de).
Snapshots: abo_snapshots/akibapass_catalog.json
Diff (new/removed/changed): Merge nach abo_changes.json["akibapass"].
Laeuft im Abo-Katalog-Check-Workflow NACH mangaplus_catalog.py.
Bewusst reine stdlib (urllib) - keine requests noetig.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

REPO = Path(os.environ.get("REPO_ROOT", str(Path.cwd())))
SNAP_DIR = REPO / "abo_snapshots"
SNAP_DIR.mkdir(exist_ok=True)
SNAP_PATH = SNAP_DIR / "akibapass_catalog.json"
CHANGES_PATH = REPO / "abo_changes.json"
URL = ("https://akibapassshop.de/collections/digital/products.json"
       "?limit=250&page={page}")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")


def scrape_akibapass():
    """Gibt {handle: {...}} zurueck (ein Eintrag je Produkt)."""
    products = {}
    page = 1
    while True:
        req = urllib.request.Request(URL.format(page=page), headers={
            "User-Agent": UA,
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8",
                                                errors="replace"))
        batch = data.get("products", [])
        if not batch:
            break
        for p in batch:
            handle = p.get("handle") or str(p.get("id"))
            variants = []
            for v in p.get("variants", []):
                variants.append({
                    "title": v.get("title"),
                    "sku": v.get("sku"),
                    "price": v.get("price"),
                    "available": bool(v.get("available")),
                })
            products[handle] = {
                "product_id": p.get("id"),
                "title": p.get("title"),
                "product_type": p.get("product_type"),
                "tags": p.get("tags"),
                "published_at": p.get("published_at"),
                "url": f"https://akibapassshop.de/products/{handle}",
                "variants": variants,
            }
        if len(batch) < 250:
            break
        page += 1
        if page > 10:            # Sicherheitsstopp (max. 2500 Produkte)
            print("akibapass: >10 Seiten - Sicherheitsstopp")
            break
    return products


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
    current = scrape_akibapass()
    if not current:
        print("akibapass: 0 Produkte erkannt - Endpunkt pruefen "
              "(Shop geaendert?)")
        return 1
    delta = diff(current)

    changes = {}
    if CHANGES_PATH.exists():
        try:
            changes = json.loads(CHANGES_PATH.read_text(encoding="utf-8"))
        except Exception:
            changes = {}
    changes["akibapass"] = delta
    CHANGES_PATH.write_text(
        json.dumps(changes, ensure_ascii=False, indent=1),
        encoding="utf-8")

    n_avail = sum(1 for p in current.values()
                  if any(v["available"] for v in p["variants"]))
    print(f"Produkte Akiba Pass Digital: {len(current)} "
          f"(davon verfuegbar: {n_avail}; neu: {len(delta['new'])}, "
          f"entfernt: {len(delta['removed'])}, "
          f"geaendert: {len(delta['changed'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
