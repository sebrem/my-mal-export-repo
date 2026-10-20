#!/usr/bin/env python3
"""
Abo-Discovery-Vorfilter (20.10.2026).

Liest die 4 Abo-Katalog-Snapshots plus mal_aliases.json und mal_manga.json,
filtert Kernmuster-Treffer (Sebastians Dauerkriterien, EN-Titel/Tags),
schliesst bereits dokumentierte oder gelesene Titel aus
(discovery_exclude.json) und schreibt die Kandidatenliste
abo_snapshots/discovery_candidates.json.

Nur VORFILTER fuer den monatlichen Abo-Discovery-Check (20./Monat):
Fund != Empfehlung - die Verifikation (Doppelkauf-Check-Sequenz gegen
Bestandsdateien + MAL-Lesestand) bleibt Aufgabe des Agenten im Check-Lauf.

Aufbau von discovery_candidates.json:
  candidates       - frische Kernmuster-Treffer je Plattform (verifizieren)
  already_tracked  - Treffer mit MAL-Eintrag (reading/PTW/PTR/...): NICHT
                     als Neufund melden, aber Status-Drift im Blick behalten
  mp_noise         - MangaPlaza-West-Romance-Rauschen (nur generische
                     Ehe-Keywords, z. B. Harlequin-Titel): ignorieren

Pure stdlib, laeuft im abgleich-Job der Abo-Katalog-Action nach
mal_aliases.py (braucht frischen mal_manga.json aus dem Repo-Checkout).
"""
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(os.environ.get("REPO_ROOT", str(Path.cwd())))
SNAP = REPO / "abo_snapshots"
OUT_PATH = SNAP / "discovery_candidates.json"
EXCLUDE_PATH = SNAP / "discovery_exclude.json"

# ---------- Kernmuster (EN, case-insensitive; Titel + JNC-Tags) ----------

TITLE_PATTERNS = [
    ("Ehe-Praemisse", re.compile(
        r"marri|bride|wedding|\bwife\b|\bhusband\b|\bspouse\b"
        r"|\bmarried\b|bridesmaid|fianc", re.I)),
    ("Gal-x-Introvert", re.compile(r"\bgyaru\b|\bgal\b", re.I)),
    ("Office/Workplace", re.compile(
        r"\boffice\b|\bboss(es)?\b|\bcoworker\b|\bcolleague\b"
        r"|workplace|salarym|\bemployees?\b|\bdesk job\b", re.I)),
    ("Yuri", re.compile(r"\byuri\b|girls[ ]?love", re.I)),
    ("Katzen", re.compile(
        r"\bcat\b|\bcats\b|kitten|\bneko\b|\bfeline\b|\bpuppy\b", re.I)),
    ("Kreaturen/Fantasy", re.compile(
        r"\belf\b|\belves\b|\bfair(y|ies)\b|\bdragon\b|\bvampire\b"
        r"|\bwitch(es)?\b|\bbeast\b|\bfox\b|\bwolf\b|\bwolves\b"
        r"|\bmonster(s)?\b|\bdemon(s)?\b|\byokai\b|\bspirit(s)?\b"
        r"|\bmermaid\b|\bharpy\b|\bgolem\b|\bunicorn\b", re.I)),
    ("Food/Slow-Life", re.compile(
        r"\bfood\b|kitchen|restaurant|dining|\bcafe\b|caf\u00e9|bakery"
        r"|cooking|\bcook\b|\bmeal(s)?\b|recipe|sweets|izakaya|ramen"
        r"|\blunch\b|\bdinner\b|\bbento\b|\bsnack\b", re.I)),
    ("Slow-Life/Iyashikei", re.compile(
        r"slow[ ]?life|\bcozy\b|\bvillage\b|countryside|country liv|farming"
        r"|\bfarm\b|healing|\bheals?\b|\bcamping\b", re.I)),
    ("Kuudere/Isolation", re.compile(
        r"kuudere|aloof|shut-in|hikikomori|introvert|quiet girl|wallflower"
        r"|\bshy\b|\bsilent\b|emotionless|deadpan", re.I)),
    ("Erwachsenen-Romance", re.compile(
        r"\b30[- ]|\b31[- ]|\b32[- ]|\b35[- ]|\b40[- ]|29[- ]year"
        r"|middle[- ]aged|\badult\b|single mother|widow|\bdivorce", re.I)),
]

JNC_TAG_PATTERNS = [
    ("Yuri", re.compile(r"yuri|girls love", re.I)),
    ("Food/Slow-Life", re.compile(r"cooking|\bfood\b|\bcats\b|slow life|cozy", re.I)),
    ("Ehe-Praemisse", re.compile(r"contract|bridal|marriage", re.I)),
    ("Office/Workplace", re.compile(r"workplace|\boffice\b", re.I)),
    ("Party-Tsuihou", re.compile(r"party|exile|banish|kick", re.I)),
]

# MangaPlaza-Rauschen: West-Romance-Marker. Titel, die NUR den generischen
# Ehe-Keyword-Treffer haben und zusaetzlich auf NOISE matchen, landen in
# mp_noise statt in den Kandidaten.
MP_NOISE = re.compile(
    r"billionaire|millionaire|sheikh|spaniard|tycoon|virginity|underwear"
    r"|lingerie|\bmistress\b|claims his|ruthless|convenient"
    r"|greek|italian", re.I)

GENERIC_PATTERNS = {"Kreaturen/Fantasy", "Party-Tsuihou"}


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def unesc(s):
    return html.unescape(s or "").strip()


def build_alias_index():
    """EN-Titel/JP/Synonyme -> {mal_id, jp} aus mal_aliases.json."""
    idx = {}
    for entry in load_json(SNAP / "mal_aliases.json", {}).values():
        mal_id = entry.get("mal_id")
        if not mal_id:
            continue
        keys = [entry.get("english") or "", entry.get("japanese") or ""]
        keys += list(entry.get("synonyms") or [])
        for key in keys:
            k = unesc(key).lower()
            if len(k) >= 5 and k not in idx:
                idx[k] = {"mal_id": mal_id,
                          "jp": entry.get("japanese") or ""}
    return idx


def build_mal_status():
    """mal_id -> {status, score} aus mal_manga.json (Feld heisst 'id'!)."""
    data = load_json(REPO / "mal_manga.json", {})
    out = {}
    for e in data.get("list", []):
        ls = e.get("list_status", {})
        out[e.get("id")] = {"status": ls.get("status"), "score": ls.get("score")}
    return out


def classify(title, tags=None):
    t = unesc(title)
    pats = [label for label, rx in TITLE_PATTERNS if rx.search(t)]
    if tags:
        blob = " ".join(str(x) for x in tags)
        pats += [label for label, rx in JNC_TAG_PATTERNS if rx.search(blob)]
    return t, sorted(set(pats))


def match_key(title):
    """Titel fuer MAL-Alias-Lookup: '[jnc] '-Praefix und ' (Manga)'
    entfernen (JNC-Listung behindert sonst den exakten EN-Vergleich)."""
    t = unesc(title)
    for prefix in ("[jnc] ", "[jnc_nina] "):
        if t.startswith(prefix):
            t = t[len(prefix):]
    if t.endswith(" (Manga)"):
        t = t[:-len(" (Manga)")]
    return t


def alias_lookup(title, idx):
    """Exakter Treffer, sonst Praefix-Containment in beide Richtungen."""
    low = match_key(title).lower()
    if low in idx:
        return idx[low]
    for key, val in idx.items():
        if low.startswith(key) or key.startswith(low):
            tail = (low[len(key):] if low.startswith(key)
                    else key[len(low):])
            if not tail or tail.lstrip(" ").startswith(("(", ":", "-", " in ")):
                return val
    return None


def main():
    exclude = [x.lower() for x in
               load_json(EXCLUDE_PATH, {}).get("title_fragments", [])]
    alias_idx = build_alias_index()
    mal_status = build_mal_status()

    sources = {
        "mangaup": (load_json(SNAP / "mangaup_catalog.json", {}), None),
        "mangaplus": (load_json(SNAP / "mangaplus_catalog.json", {}), None),
        "mangaplaza": (load_json(SNAP / "mangaplaza_readinfull.json", {}),
                       None),
        "jnc": (load_json(SNAP / "jnc_series.json", {}), "tags"),
    }

    candidates = {k: [] for k in sources}
    already_tracked = []
    mp_noise = []

    for platform, (catalog, tag_field) in sources.items():
        for raw_title, meta in catalog.items():
            tags = meta.get(tag_field) if tag_field else None
            title, pats = classify(raw_title, tags)
            if not pats:
                continue
            # Generische Treffer allein sind kein Kandidat (Rauschen):
            # Kreaturen-Keyword oder Party-Tsuihou-Tag ohne jedes
            # spezifischere Muster (z. B. Shounen-Battle-Titel).
            if not (set(pats) - GENERIC_PATTERNS) and len(pats) < 2:
                continue
            low = title.lower()
            if any(frag in low for frag in exclude):
                continue
            url = meta.get("title_url") or ""
            if platform == "jnc" and not url and meta.get("slug"):
                url = "https://j-novel.club/series/" + meta["slug"]
            entry = {
                "title": title,
                "title_id": str(meta.get("title_id") or meta.get("slug") or ""),
                "title_url": url,
                "platform": platform,
                "patterns": pats,
                "mal_id": None,
                "mal_status": None,
                "mal_score": None,
            }
            hit = alias_lookup(title, alias_idx)
            if hit:
                entry["mal_id"] = hit["mal_id"]
                st = mal_status.get(hit["mal_id"])
                if st:
                    entry["mal_status"] = st["status"]
                    entry["mal_score"] = st["score"]
            if entry["mal_status"]:
                already_tracked.append(entry)
            elif (platform == "mangaplaza" and MP_NOISE.search(title)
                    and set(pats) <= {"Ehe-Praemisse", "Office/Workplace"}):
                # West-Romance-Rauschen: nur generische Ehe-/Office-Keywords
                mp_noise.append(entry)
            else:
                candidates[platform].append(entry)

    for platform in candidates:
        # Mehrfach-Muster-Treffer zuerst (beste Kandidaten oben)
        candidates[platform].sort(
            key=lambda e: (-len(e["patterns"]), e["title"]))

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": ("Vorfilter fuer Abo-Discovery-Check (20./Monat). Fund != "
                 "Empfehlung: Kandidaten vom Agenten verifizieren "
                 "(Doppelkauf-Check-Sequenz + MAL-Lesestand nach "
                 "Format-Lesarten). already_tracked nicht als Neufund "
                 "melden (Status-Drift pruefen). mp_noise ignorieren."),
        "counts": {
            "candidates": sum(len(v) for v in candidates.values()),
            "already_tracked": len(already_tracked),
            "mp_noise": len(mp_noise),
        },
        "candidates": candidates,
        "already_tracked": already_tracked,
        "mp_noise": mp_noise,
    }
    OUT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("Abo-Discovery: {0} Kandidaten (already_tracked: {1}, "
          "mp_noise: {2})".format(result["counts"]["candidates"],
                                   result["counts"]["already_tracked"],
                                   result["counts"]["mp_noise"]))
    for platform in ("jnc", "mangaplaza", "mangaplus", "mangaup"):
        if candidates[platform]:
            print("  {0}: {1}".format(platform, len(candidates[platform])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
