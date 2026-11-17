#!/usr/bin/env python3
"""
jnc_anschluss.py v2 – JNC-Anschluss-Klassifikation (14.11.2026; v2 17.11.2026:
JNC-Nina-Merge).

Verbindet die JNC-Verfuegbarkeits-Snapshots bandgenau mit Sebastians
Besitz-/Lesestand (Einheiten-Regel: JNC-Einheit = VOLUME, Haeppchen sind
nicht kapitel-mappbar; MAL-progress = Kapitel ist fuer LN unbrauchbar).

Quellen:
- abo_snapshots/jnc_availability.json      (JNC USA, labs.j-novel.club)
- abo_snapshots/jnc_nina_availability.json (JNC Nina, optional; v2 17.11. –
  Skript laeuft auch ohne, wenn der Nina-Step noch fehlgeschlagen ist)
- abo_snapshots/own_volumes.json           (Sebastian/Agent gepflegt;
  Edition-Regel: Adaptionen/Uebersetzungen content-gleich.
  v2-Konvention: Nina-Besitz/Lesestand als eigene Serie mit Key =
  Nina-SLUG, "platform": "jnc_nina" (Schlüsselkollision mit JNC-Slugs
  moeglich, deshalb nur nina_-Key dort eintragen, wo es um Nina geht)
- mal_manga_index.json                     (MAL-Lesestand; "volumes"-Feld)

Ausgabe: abo_snapshots/jnc_anschluss.json
Klassifikation je Serie (nur fuer Serien mit OWN-Eintrag oder Lesestand>0
sowie alle mit Library-Zugang als Basisklassifikation):
- START_OK     Bd. 1 im Abo, keine Besitz-Kollision
- FORTSETZUNG  Anschlussband (max(own)+1 bzw. volumes_read+1) im Abo verfuegbar
- KONFLIKT     Abo-Baende bereits geklesen/gelesen (content-gleich) - NICHT
               als "jetzt lesbar" melden (Doppelkonsum-Regel, Skill anime-manga-abgleich)
- LUECKE       Abo beginnt erst spaeter (min verfuegbares Bd. > 1 und kein
               eigener Anschlussstand dabei)
- NUR-PP       kein Library-Band, nur Pre-Pub-Fenster
Plus "eilt"-Flag: aktive Pre-Pubs, deren Ablauf (= publishing,
E-Book-Release; Part-expiration == publishing, 16.11. API-verifiziert)
in <= 14 Tagen faellt.
Nina-Serien erscheinen mit Key "nina:<slug>" und "platform": "jnc_nina";
Nina hat KEINE Backlist-Library - Library-Treffer dort = Loan/
membership-abhaengig (Semantik: manga-sammlung/abo-katalog.md,
Nina-Pre-Pub-Lane-Block 17.11.2026).
Fund != Empfehlung: die Klassifikation ist Datenbasis, keine Empfehlung.
Reine stdlib. continue-on-error im Workflow.
"""
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("REPO_ROOT", str(Path.cwd())))
SNAP = ROOT / "abo_snapshots"
AVAIL = SNAP / "jnc_availability.json"
AVAIL_NINA = SNAP / "jnc_nina_availability.json"
OWN = SNAP / "own_volumes.json"
MALIDX = ROOT / "mal_manga_index.json"
OUT = SNAP / "jnc_anschluss.json"
EILT_DAYS = 14


def classify(avail, own_vols, mal_read):
    lib = set(avail.get("library_vols") or [])
    pp = {}
    for p in avail.get("prepub_active") or []:
        if p.get("number") is None:
            continue
        # DEFENSIV: PP-Baende mit publishing in der Vergangenheit sind
        # abgelaufen (expiration == publishing) - aus dem woechentlichen
        # Snapshot herausfiltern, egal was der Snapshot noch enthaelt.
        pub = p.get("publishing")
        if pub:
            try:
                if date.fromisoformat(pub[:10]) < date.today():
                    continue
            except ValueError:
                pass
        pp[p["number"]] = pub
    avail_vols = lib | set(pp)
    if not avail_vols:
        return "NUR-PP", {}
    own = set(own_vols)
    read = mal_read or 0
    own_max = max(own) if own else None
    # Anschluss-Stand: hoechster besessener Band, sonst MAL-Bandlesung
    stand = max([x for x in (own_max, read) if x]) if (own or read) else None
    overlap = sorted(own & (lib | set(pp)))
    new_vols = avail_vols - own
    details = {
        "lib_vols": sorted(lib),
        "pp_vols": sorted(pp),
        "own_vols": sorted(own),
        "mal_volumes_read": read,
        "overlap": overlap,
    }
    if not new_vols:
        # Alles was das Abo bietet, ist bereits besessen (content-gleich)
        return "KONFLIKT", details
    if stand is not None and (stand + 1) in new_vols:
        # Anschlussband verfuegbar - ggf. mit Ueberschneidung zu Besitz
        return "FORTSETZUNG", details
    if overlap and stand is not None:
        # Nur bereits besessene Teile im Abo, kein Anschlussband
        return "KONFLIKT", details
    if min(new_vols) > 1:
        return "LUECKE", details
    return "START_OK", details


def load_own():
    if not OWN.exists():
        return {}
    try:
        return json.loads(OWN.read_text(encoding="utf-8")).get("series", {})
    except Exception:
        return {}


def load_mal():
    mal_vols, mal_status = {}, {}
    if MALIDX.exists():
        try:
            for e in json.loads(MALIDX.read_text(encoding="utf-8")).get("list", []):
                mal_vols[e.get("id")] = e.get("volumes") or 0
                mal_status[e.get("id")] = e.get("status")
        except Exception:
            pass
    return mal_vols, mal_status


def process_platform(avail, own, mal_vols, mal_status, today,
                    platform=None, key_prefix=""):
    """Verarbeitet einen Verfuegbarkeits-Snapshot; platform=None = JNC USA."""
    out_entries, counts = {}, {}
    for slug, av in sorted(avail.items()):
        if slug.startswith("_"):
            continue   # Meta-Keys (_api_base, _generated_at) ueberspringen
        o = own.get(slug) or {}
        mid = o.get("mal_id")
        read = mal_vols.get(mid, 0) if mid else 0
        klass, det = classify(av, o.get("volumes") or [], read)
        eilt = []
        for p in av.get("prepub_active") or []:
            pub = p.get("publishing")
            if not pub:
                continue
            try:
                exp = date.fromisoformat(pub[:10])   # Ablauf = publishing
            except ValueError:
                continue
            if 0 <= (exp - today).days <= EILT_DAYS:
                eilt.append({"band": p.get("number"),
                             "ablauf_ca": exp.isoformat()})
        counts[klass] = counts.get(klass, 0) + 1
        entry = {
            "title": av.get("title"),
            "klassifikation": klass,
            "mal_id": mid,
            "mal_status": mal_status.get(mid),
            "own_platform": o.get("platform"),
            "own_edition": o.get("edition"),
            "eilt": eilt,
            **det,
        }
        if platform:
            entry["platform"] = platform
        out_entries[key_prefix + slug] = entry
    return out_entries, counts


def main():
    if not AVAIL.exists():
        print("jnc_anschluss: jnc_availability.json fehlt - "
              "erst JNC-Verfuegbarkeits-Step laufen lassen")
        return 1
    avail = json.loads(AVAIL.read_text(encoding="utf-8"))
    own = load_own()
    mal_vols, mal_status = load_mal()
    today = date.today()

    out_entries, counts = process_platform(
        avail, own, mal_vols, mal_status, today)

    # v2 17.11.2026: JNC-Nina-Merge (optionaler Snapshot)
    nina_entries, nina_counts = {}, {}
    if AVAIL_NINA.exists():
        try:
            avail_nina = json.loads(AVAIL_NINA.read_text(encoding="utf-8"))
            nina_entries, nina_counts = process_platform(
                avail_nina, own, mal_vols, mal_status, today,
                platform="jnc_nina", key_prefix="nina:")
            out_entries.update(nina_entries)
            for k, v in nina_counts.items():
                counts[k] = counts.get(k, 0) + v
        except Exception as e:
            print(f"jnc_anschluss: Nina-Merge fehlgeschlagen ({e}) - "
                  f"nur JNC-USA-Klassifikation geschrieben")
    else:
        print("jnc_anschluss: jnc_nina_availability.json fehlt "
              "(Nina-Step laeuft noch nicht / fehlgeschlagen) - "
              "nur JNC-USA-Klassifikation")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": counts,
        "note": "JNC-Anschluss-Klassifikation (START_OK/FORTSETZUNG/KONFLIKT/"
                "LUECKE/NUR-PP); KONFLIKT = Abo-Baende content-gleich bereits "
                "besessen - nie als 'jetzt lesbar' melden; Einheit = Volume; "
                "Fund != Empfehlung. Seit v2 (17.11.): Nina-Serien mit Key "
                "'nina:<slug>', platform jnc_nina, Pre-Pub-Abo ohne Backlist-Library.",
        "series": out_entries,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    n_eilt = sum(1 for e in out_entries.values() if e["eilt"])
    print("JNC-Anschluss: %d Serien (%s; davon Nina: %d); EILT-Serien: %d -> %s"
          % (len(out_entries),
             ", ".join("%s=%d" % kv for kv in sorted(counts.items())),
             len(nina_entries), n_eilt, OUT.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
