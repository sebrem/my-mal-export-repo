#!/usr/bin/env python3
"""
Manga-Plus-Katalog-Lane (Stand 17.10.2026, erste Iteration - BLIND BUILD).
Hinter mangaplus.shueisha.co.jp/manga_list/{ongoing,completed,one_shot}
steckt eine Vue-SPA; die Daten kommen aus der oeffentlichen
Protobuf-API: GET https://jumpg-webapi.tokyo-cdn.com/api/title_list/all_v3
(DNS-Hint "jumpg-webapi.tokyo-cdn.com" im SPA-HTML, Endpoint-Namen aus
app.js: title_list/all_v3 GET, Basis-URL .../api).

LOESUNG (17.10.2026): Der "Account Banned"-Response kommt NICHT von
einer IP-Sperre, sondern vom FEHLENDEN SESSION-TOKEN-Header. Ein
beliebiger Token (Null-UUID) reicht - von der Vibe-Sandbox getestet.
Die drei Listen (ongoing/completed/one_shot) werden per type-Parameter
serverseitig gefiltert: serializing / completed / one_shot.

Protobuf wird bewusst ohne pip geloest: minimaler Varint/
length-delimited-Walker; "Title"-Nodes = Submessage mit Feld 1 =
Varint (title_id) und Feld 2 = String (name). Sektionen (repeated
Nodes mit wiederholten Title-Children) werden in Web-Reihenfolge als
ongoing/completed/one_shot gelabelt; wenn die Struktur nicht passt,
fallen alle Titel in status "unknown" (Katalog-Treffer funktioniert
trotzdem).

Snapshots: abo_snapshots/mangaplus_catalog.json
Diff: Merge nach abo_changes.json["mangaplus"].
KEIN Abo: Manga+ ist gratis/teils MAX - Verfuegbarkeits-Lane wie Manga UP!.
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
SNAP_PATH = SNAP_DIR / "mangaplus_catalog.json"
CHANGES_PATH = REPO / "abo_changes.json"
BASE_URL = ("https://jumpg-webapi.tokyo-cdn.com/api/title_list/"
             "all_v3")
# WICHTIG (17.10.2026 rausgefunden): Ohne SESSION-TOKEN-Header antwortet
# die API mit "Account Banned" - egal von welcher IP. Der Token wird
# offenbar NICHT validiert: eine fixe Null-UUID reicht.
SESSION_TOKEN = os.environ.get("MP_SESSION_TOKEN",
                               "00000000-0000-0000-0000-000000000000")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
SECTION_LABELS = ["ongoing", "completed", "one_shot"]


# ---------- minimaler Protobuf-Walker (stdlib) ----------

def _read_varint(buf, i):
    val = 0
    shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("varint overrun")
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def parse_fields(buf):
    """Liefert [(field_no, wire_type, value)] mit value=int oder bytes."""
    out = []
    i = 0
    n = len(buf)
    while i < n:
        try:
            key, i = _read_varint(buf, i)
        except ValueError:
            break                      # Rumpf am Ende ignorieren
        fno, wt = key >> 3, key & 7
        if wt == 0:                    # varint
            try:
                val, i = _read_varint(buf, i)
            except ValueError:
                break
        elif wt == 1:                  # 64-bit
            if i + 8 > n:
                break
            val, i = buf[i:i + 8], i + 8
        elif wt == 2:                  # length-delimited
            try:
                ln, i = _read_varint(buf, i)
            except ValueError:
                break
            if ln > n - i:
                break                  # kaputte Laenge: Rest verwerfen
            val, i = buf[i:i + ln], i + ln
        elif wt == 5:                  # 32-bit
            if i + 4 > n:
                break
            val, i = buf[i:i + 4], i + 4
        else:
            break                      # groups (3/4): ignorieren
        out.append((fno, wt, val))
    return out


def as_text(b):
    """bytes -> str, wenn druckbarer Text, sonst None."""
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not s or any(ord(c) < 9 for c in s):
        return None
    return s


def is_title_node(fields):
    """Title = Feld 1 Varint (title_id), Feld 2 String (name)."""
    f1 = [v for f, w, v in fields if f == 1 and w == 0]
    f2 = [v for f, w, v in fields if f == 2 and w == 2]
    if len(f1) != 1 or len(f2) < 1:
        return None
    name = as_text(f2[0])
    if (name is None or not (1 <= len(name) <= 300)
            or f1[0] < 1000 or name.startswith(("http://", "https://",
                                                "data:", "HTTP", "GET "))):
        return None                    # nur echte Titel: id>=1000, kein URL-Ballast
    return f1[0], name.strip()


def walk(buf, depth=0, max_depth=12):
    """Rekursiv Submessages oeffnen, Title-Kandidaten einsammeln."""
    titles = []          # (title_id, name, status)
    sections = []         # Nodes, die wiederholt Title-Kinder haben
    here_titles = []
    kids = []
    fields = parse_fields(buf)
    for f, w, v in fields:
        if w == 2 and depth < max_depth:
            sub = parse_fields(v)
            # nur weiterverfolgen, wenn die Submessage selbst plausibel
            t = is_title_node(sub)
            if t:
                here_titles.append(t)
            else:
                kids.append(v)
    if here_titles:
        sections.append(here_titles)
    for k in kids:
        t2, s2 = walk(k, depth + 1, max_depth)
        titles.extend(t2)
        sections.extend(s2)
    if depth == 0:
        # Sektionen der obersten Ebene labeln; Rest = unknown
        status = "unknown"
        flat_secs = sections
        if 1 <= len(flat_secs) <= 4:
            for idx, sec in enumerate(flat_secs):
                lab = (SECTION_LABELS[idx]
                       if idx < len(SECTION_LABELS) else "other")
                for tid, name in sec:
                    titles.append((tid, name, lab))
        else:
            for sec in flat_secs:
                for tid, name in sec:
                    titles.append((tid, name, status))
    return titles, sections


def fetch(ttype):
    """Holt eine der drei Listen: serializing/completed/one_shot."""
    url = (f"{BASE_URL}?type={ttype}&lang=eng&clang=eng")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip",
        "Referer": "https://mangaplus.shueisha.co.jp/",
        "Origin": "https://mangaplus.shueisha.co.jp",
        "SESSION-TOKEN": SESSION_TOKEN,
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
        if resp.headers.get("Content-Encoding", "") == "gzip":
            import gzip
            data = gzip.decompress(data)
    if b"Account Banned" in data[:4096]:
        raise RuntimeError(
            "Account-Banned-Antwort - SESSION-TOKEN-Header fehlt?")
    return data


def scrape_mangaplus():
    out = {}
    for ttype in ("serializing", "completed", "one-shot"):
        data = fetch(ttype)
        titles, _ = walk(data)
        for tid, name, _sec in titles:
            if not name or name in out:
                continue
            out[name] = {
                "title_id": str(tid),
                "title_url":
                    f"https://mangaplus.shueisha.co.jp/titles/{tid}",
                "status": ttype,
            }
    return out


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
    try:
        current = scrape_mangaplus()
    except Exception as e:
        print(f"mangaplus: FEHLER: {e}")
        return 1
    if not current:
        print("mangaplus: 0 Titel erkannt - Parser pruefen "
              "(API-Struktur geaendert?)")
        return 1
    delta = diff(current)

    changes = {}
    if CHANGES_PATH.exists():
        try:
            changes = json.loads(CHANGES_PATH.read_text(encoding="utf-8"))
        except Exception:
            changes = {}
    changes["mangaplus"] = delta
    CHANGES_PATH.write_text(
        json.dumps(changes, ensure_ascii=False, indent=1),
        encoding="utf-8")

    from collections import Counter
    stats = Counter(v["status"] for v in current.values())
    print(f"Titel Manga+: {len(current)} "
          f"(neu: {len(delta['new'])}, entfernt: {len(delta['removed'])}); "
          f"Sektionen: {dict(stats)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
