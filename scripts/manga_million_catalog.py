#!/usr/bin/env python3
"""Manga Million (Shueisha) Katalog-Harvest.

Quelle: https://mangamillion.shueisha.co.jp (offizielle Shueisha-100-Jahre-Plattform,
kostenlos, weltweit, ohne Registrierung; Titel-Katalog 2026-11-03: 397 Titel).

Die Seite ist eine Next.js-App ohne serverseitig gerenderte Katalogdaten; die
interne API (api.mangamillion.shueisha.co.jp) benoetigt ein Geraete-Token
(/api/register -> ausserhalb des Browsers "Forbidden"). Stabiler Weg:
  1. /sitemap.xml -> je Locale eine Sitemap mit allen /<locale>/title/<id>-URLs
  2. je Titel-Id die Titel-Seite laden und den <title>/og:title-Tag extrahieren
     (Format: "<Titelname> - <Autor(en)> | MANGA MILLION")

Sprach-Verfuegbarkeit (v2, Stand 2026-11-03):
  - Die Sitemap listet je Locale ALLE Titel, unabhaengig davon, in welchen
    Sprachen die Kapitel verfuegbar sind (DE- und EN-Sitemap sind identisch).
  - og:Titles sind NUR ein Namens-Hinweis (de_differs), KEIN Sprach-Beleg.
  - Echte Kapitel-Sprachen liefert die offene API-Endpunkt
      GET api.mangamillion.shueisha.co.jp/api/title_credit
          ?original_title_id=<id>&service_language=de&avif_enable=false
          &access_token=00000-00000-00000-00000-00000
    (Dummy-Token genuegt; Response ist protobuf mit Klartext-Bloecken
     "Deutsch（#001～#012）" je Sprache). Geschuetzte Endpunkte
     (title_detail, chapter_list, register) brauchen echte Device-Tokens.
  - Kennzahlen 2026-11-03: 397 Titel, 70 mit Deutsch-Kapiteln, 84 mit English.
    Regulaerer Harvest erfasst damit DE-/EN-Range UND Kapitelzahl je Titel.

Outputs (Repo-Root, konventionsgemaess wie akibapass_catalog):
  abo_snapshots/manga_million_catalog.json  Voll-Katalog
  abo_snapshots/manga_million_changes.json  Delta (neu/entfernt/umbenannt) gg. vorherigem Stand
  abo_snapshots/manga_million_index.json    Slim-Index (id -> Namen je Locale)
"""
import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://mangamillion.shueisha.co.jp"
API = "https://api.mangamillion.shueisha.co.jp"
DUMMY_TOKEN = "00000-00000-00000-00000-00000"
LOCALES = ["en", "de"]
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) manga-million-harvest/1.0"}
OUT_DIR = Path("abo_snapshots")
CATALOG = OUT_DIR / "manga_million_catalog.json"
CHANGES = OUT_DIR / "manga_million_changes.json"
INDEX = OUT_DIR / "manga_million_index.json"

def fetch(url, tries=3, timeout=30):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            if i == tries - 1:
                print(f"WARN fetch failed {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2 * (i + 1))

LANGS_RE = re.compile(r"([^\n\x12\x1a\x22]{2,40}?)（(#[^）]+)）")
# Feste Sprach-Whitelist: protobuf-Keys enthalten Laengenbytes/Publisher-Reste,
# z.B. "\x18Deutsch" oder "PaniniuDeutsch" -> Whitelist-Treffer am Key-Ende.
LANG_WHITELIST = [
    "Deutsch", "English", "Español (Argentina)", "Español (México)", "Español",
    "Français", "Italiano", "Português (Brasil)", "Português", "Русский язык",
    "繁體中文", "简体中文", "한국어", "ภาษาไทย", "Tiếng Việt", "Polski", "Türkçe",
    "Suomi", "Čeština", "Svenska", "Bahasa Indonesia", "Bahasa Melayu", "Tagalog",
    "हिन्दी", "Ελληνικά", "العربية", "ភាសាខ្មែរ",
]

def parse_credits(raw: bytes):
    """title_credit-protobuf -> {Sprache: Range '#001～#012'} (Whitelist-normiert)."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    out = {}
    for key, rng in LANGS_RE.findall(text):
        for lang in LANG_WHITELIST:
            if key.endswith(lang) or key == lang:
                out.setdefault(lang, rng)
                break
    return out

def chapter_count(rng):
    nums = [int(n) for n in re.findall(r"#(\d+)", rng or "")]
    return max(nums) if nums else 0

def fetch_title_credit(tid):
    url = (f"{API}/api/title_credit?original_title_id={tid}"
           f"&service_language=de&avif_enable=false&access_token={DUMMY_TOKEN}")
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            return parse_credits(r.read())
    except Exception as e:
        print(f"WARN title_credit {tid}: {e}", file=sys.stderr)
        return {}

def title_ids():
    idx = fetch(BASE + "/sitemap.xml")
    if not idx:
        raise SystemExit("sitemap.xml nicht erreichbar")
    locmap = {}
    for loc in re.findall(r"<loc>([^<]+)</loc>", idx):
        m = re.search(r"/sitemap-([a-zA-Z-]+)-\d+-\d+\.xml$", loc)
        if m:
            locmap[m.group(1).lower()] = loc
    out = {}
    for locale in LOCALES:
        sm = locmap.get(locale)
        if not sm:
            print(f"WARN keine Sitemap fuer Locale {locale}", file=sys.stderr)
            continue
        xml = fetch(sm)
        if not xml:
            continue
        ids = sorted({int(i) for i in re.findall(r"/title/(\d+)", xml)})
        out[locale] = ids
    return out

def fetch_title(args):
    tid, locale = args
    html = fetch(f"{BASE}/{locale}/title/{tid}")
    if not html:
        return tid, locale, None
    m = re.search(r"<title>([^<|]+)", html)
    if not m:
        return tid, locale, None
    raw = m.group(1).strip()
    # "Name - Autor | MANGA MILLION" -> Name, Autor trennen (letztes " - ")
    name, author = raw, ""
    mm = re.match(r"^(.*) - ([^-]+)$", raw)
    if mm:
        name, author = mm.group(1).strip(), mm.group(2).strip()
    return tid, locale, {"raw": raw, "name": name, "author": author}

def main():
    ids_by_locale = title_ids()
    all_ids = sorted({i for ids in ids_by_locale.values() for i in ids})
    print(f"Titel-IDs gesamt: {len(all_ids)}; je Locale: "
          + ", ".join(f"{k}={len(v)}" for k, v in ids_by_locale.items()))
    jobs = [(tid, loc) for tid in all_ids for loc in LOCALES if loc in ids_by_locale and tid in ids_by_locale[loc]]
    results = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for tid, locale, info in ex.map(fetch_title, jobs):
            if info:
                results.setdefault(tid, {"id": tid})[locale] = info
    # v2: Kapitel-Sprachen je Titel (title_credit, Dummy-Token)
    credits = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for tid, langs in zip(all_ids, ex.map(fetch_title_credit, all_ids)):
            credits[tid] = langs
    titles = []
    for tid in all_ids:
        e = results.get(tid, {"id": tid})
        en = e.get("en", {})
        de = e.get("de", {})
        langs = credits.get(tid, {})
        de_rng = langs.get("Deutsch")
        en_rng = langs.get("English")
        titles.append({
            "id": tid,
            "name": en.get("name") or de.get("name"),
            "author": en.get("author") or de.get("author"),
            "name_de": de.get("name"),
            "de_differs": bool(de.get("name") and de.get("name") != en.get("name")),
            "url_en": f"{BASE}/en/title/{tid}",
            "url_de": f"{BASE}/de/title/{tid}",
            "languages": langs,
            "de_range": de_rng,
            "de_chapters": chapter_count(de_rng),
            "en_range": en_rng,
            "en_chapters": chapter_count(en_rng),
        })
    titles.sort(key=lambda t: t["id"])
    de_named = sum(1 for t in titles if t["de_differs"])
    de_titles = sum(1 for t in titles if t["de_range"])
    en_titles = sum(1 for t in titles if t["en_range"])

    # Delta gg. vorherigem Stand
    changes = {"generated_at": None, "new": [], "removed": [], "renamed": [], "language_changes": []}
    if CATALOG.exists():
        try:
            prev = {t["id"]: t for t in json.loads(CATALOG.read_text())["titles"]}
            curr = {t["id"]: t for t in titles}
            changes["new"] = [curr[i]["name"] for i in sorted(curr.keys() - prev.keys())]
            changes["removed"] = [prev[i]["name"] for i in sorted(prev.keys() - curr.keys())]
            changes["renamed"] = [
                {"id": i, "from": prev[i]["name"], "to": curr[i]["name"]}
                for i in sorted(prev.keys() & curr.keys())
                if prev[i]["name"] != curr[i]["name"]
            ]
            # v2: Sprach-/Range-Aenderungen (neue Sprache oder andere Range)
            changes["language_changes"] = [
                {"id": i, "name": curr[i]["name"], "old": prev[i].get("languages"),
                 "new": curr[i].get("languages")}
                for i in sorted(prev.keys() & curr.keys())
                if prev[i].get("languages") != curr[i].get("languages")
            ]
        except Exception as e:
            print(f"WARN Delta-Vergleich fehlgeschlagen: {e}", file=sys.stderr)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    OUT_DIR.mkdir(exist_ok=True)
    catalog_doc = {
        "generated_at": now,
        "source": BASE,
        "note": "Titel-Katalog aus Sitemap+og:title; Kapitel-Sprachen aus /api/title_credit (Dummy-Token, protobuf); free limited-time service",
        "count": len(titles),
        "de_named_titles": de_named,
        "de_titles": de_titles,
        "en_titles": en_titles,
        "titles": titles,
    }
    CATALOG.write_text(json.dumps(catalog_doc, ensure_ascii=False, indent=1))
    changes["generated_at"] = now
    CHANGES.write_text(json.dumps(changes, ensure_ascii=False, indent=1))
    index_doc = {
        "generated_at": now,
        "count": len(titles),
        "note": "Slim-Index: id -> name/name_de; Voll-Katalog in manga_million_catalog.json",
        "titles": {str(t["id"]): {"name": t["name"], "name_de": t["name_de"],
                                  "de_differs": t["de_differs"],
                                  "de_chapters": t["de_chapters"],
                                  "en_chapters": t["en_chapters"]} for t in titles},
    }
    INDEX.write_text(json.dumps(index_doc, ensure_ascii=False, indent=1))
    print(f"OK: {len(titles)} Titel ({de_named} mit eigenem DE-Namen; "
          f"{de_titles} mit DE-Kapiteln, {en_titles} mit EN), Delta: "
          f"{len(changes['new'])} neu, {len(changes['removed'])} entfernt, "
          f"{len(changes['renamed'])} umbenannt, {len(changes.get('language_changes', []))} Sprach-Aenderungen")

if __name__ == "__main__":
    main()
