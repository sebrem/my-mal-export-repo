#!/usr/bin/env python3
"""Actions-Waechter v4 (stdlib only).

Taeglicher Watchdog ueber alle geplanten Workflows des Repos. Pro erwartetem
Workflow wird geprueft, ob seit dessen letztem erwarteten Cron-Termin ein
erfolgreicher Run existiert. Ergebnisse:
  - ok      : Erfolg-Run seit letztem erwarteten Termin vorhanden
  - late    : Erfolg-Run vorhanden, aber deutlich verspaetet (v4, s.u.)
  - failed  : Run(s) vorhanden, aber kein Erfolg (conclusion != success)
  - missing : gar kein Run seit erwartetem Termin
  - error   : Workflow/API nicht abrufbar
  - pending : Workflow ist noch nicht lange genug live (v3)

Alarmweg: GitHub-Issue mit Label "actions-waechter" (ein offenes
Sammel-Issue, aktualisiert statt vermehrt; wird bei Erholung geschlossen).
Zusatz: actions_status.json als Heartbeat-/Status-Datei (Commit macht der
Workflow, nicht das Skript - dort mit if: always(), damit der Status auch
bei rot abgelegten Check landet).

v4-Aenderungen (26.10.2026, nach Befund "MAL Sync 19.10. 5h42 verspaetet,
zaehlte als ok"):
  1. Verspaetungs-Messung: delay = Startzeit - erwarteter Termin. Ueberschreitet
     der laengste relevante Delay LATE_ALARM_HOURS (Default 4), wird der
     Workflow als "late" gefuehrt und alarmiert wie "missing". Grund: die
     4h-GRACE in v3 wirkte nur nach UNTEN (Run darf nicht vor Termin-4h
     starten), hatte aber keine Obergrenze - 5-6h-Verspaetungen blieben
     unsichtbar, obwohl sie strukturell wiederkehren.
  2. Manueller Catch-up zaehlt: v3 filterte strikt auf event=schedule, d.h.
     ein bewusst nachgezogener manueller Dispatch (wie MAL Sync 26.10.)
     alarmierte trotzdem als "missing". v4 prueft bei fehlendem Scheduled-Run
     zusaetzlich Runs JEDES Events; existiert seit dem erwarteten Termin ein
     erfolgreicher Run (z.B. workflow_dispatch), gilt der Workflow als "ok",
     mit Vermerk "(manuell)" im Detail.

v3-Fix (23.10.2026, Erst-Alarm-Analyse): EXPECTED-Eintraege bekommen ein
optionales "since" (Geburtsdatum/Live-Datum des Workflows). Liegt der
letzte erwartete Termin VOR dem Live-Datum, ist der Status "pending" statt
"missing" und kein Alarm-Grund. Ausserdem monatliche Crons via "dom"
(theme-seiyuu-harvest.yml), streaming-rollcheck.yml in EXPECTED.

Jitter-Behandlung: Ein Run zaehlt als zugehoerig, wenn er ab
(termin - GRACE_HOURS) gestartet ist. Verzoegerungen bis LATE_ALARM_HOURS
bleiben "ok", darueber "late" (neu in v4); laeuft eine Action erst NACH dem
Watchdog, erscheint sie spaetestens beim naechsten Watchdog-Lauf.
"""

import json
import os
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

REPO = os.environ.get("WAECHTER_REPO", "sebrem/my-mal-export-repo")
TOKEN = os.environ.get("WAECHTER_TOKEN", "")
LABEL = "actions-waechter"
GRACE_HOURS = 4
# v4: Verspaetungs-Schwelle in Stunden - Erfolg-Runs, die deutlich nach dem
# erwarteten Termin starten, alarmieren als "late" statt still als "ok".
LATE_ALARM_HOURS = 4
STATUS_FILE = "actions_status.json"

# Erwartete scheduled Workflows. Wochentage: Mo=0 .. So=6 (UTC).
# "dom" = Tag des Monats fuer monatliche Crons (z.B. 1 = jeden 1.).
# "since" = Live-Datum (ISO, UTC) des Workflows: Termine davor -> pending,
#           kein Alarm (verhindert False-Alarme fuer junge Workflows).
# Bei neuen/verschobenen Crons hier nachpflegen - single source of truth.
EXPECTED = [
    {"file": "mal-export.yml",            "name": "MAL Sync",               "days": [0], "hour": 4, "minute": 17},
    {"file": "mal_neighbours.yml",        "name": "MAL Friends Candidates",  "days": [0], "hour": 4, "minute": 35,
     "since": "2026-10-21T12:30:00+00:00"},  # live 21.10.; erster Cron: Mo 26.10. 04:35
    {"file": "anilist_export.yml",        "name": "AniList Export",           "days": [0], "hour": 4, "minute": 47},
    {"file": "abo-katalog-check.yml",     "name": "Abo-Katalog-Check",        "days": [0], "hour": 5, "minute": 17},
    {"file": "verlags-announcements.yml", "name": "Verlags-Announcements",   "days": [2], "hour": 5, "minute": 47,
     "since": "2026-10-22T08:00:00+00:00"},  # live 22.10.; erster Cron: Mi 28.10. 05:47
    {"file": "release-kalender.yml",      "name": "Release-Kalender",         "days": [3], "hour": 4, "minute": 30,
     "since": "2026-10-22T08:15:00+00:00"},  # live 22.10. ~08:15; erster Cron: Do 29.10. 04:30
    {"file": "streaming-rollcheck.yml",   "name": "Streaming Rollcheck",      "days": [0, 1, 2, 3, 4, 5, 6], "hour": 3, "minute": 17,
     "since": "2026-10-23T13:24:00+00:00"},  # live 23.10. 13:24; erster Cron: Fr 24.10. 03:17
    {"file": "theme-seiyuu-harvest.yml",  "name": "Theme & Seiyuu Harvest",   "dom": 1, "hour": 5, "minute": 3,
     "since": "2026-10-22T13:30:00+00:00"},  # live 22.10.; erster Cron: So 01.11.2026 05:03
]


class ApiError(Exception):
    """GitHub-API-Fehler inkl. Antwortkoerper (darin steht die Ursache)."""


def api(path, method="GET", body=None, params=None):
    """GitHub REST-API-Call mit Token. Liefert geparstes JSON."""
    url = "https://api.github.com" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers={
        "Authorization": "Bearer " + TOKEN,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "actions-waechter",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="replace")[:300]
        except Exception:
            pass
        raise ApiError(f"HTTP {e.code} bei {method} {path}: {detail}") from e
    except Exception as e:   # URLError, Timeout, SSL - auch ApiError melden
        raise ApiError(f"{type(e).__name__} bei {method} {path}: {e}") from e
    return json.loads(raw) if raw else {}


def last_expected_trigger(now, wf):
    """Letzter erwarteter Cron-Termin <= now.
    Weekly ("days"): sucht bis 8 Tage zurueck.
    Monthly ("dom"): sucht bis 40 Tage zurueck."""
    hour, minute = wf["hour"], wf["minute"]
    if "dom" in wf:
        dom = wf["dom"]
        for back in range(41):
            cand = (now - timedelta(days=back)).replace(
                hour=hour, minute=minute, second=0, microsecond=0)
            if cand <= now and cand.day == dom:
                return cand
        return None
    days = wf.get("days", [])
    for back in range(8):
        cand = (now - timedelta(days=back)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        if cand <= now and cand.weekday() in days:
            return cand
    return None


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def check_workflow(wf, now):
    """Prueft einen erwarteten Workflow. Rueckgabe: dict mit status + Details."""
    expected_at = last_expected_trigger(now, wf)
    if expected_at is None:
        return {"name": wf["name"], "status": "error",
                "detail": "kein erwarteter Termin berechenbar (Konfig?)",
                "expected": None}

    # v3: Termin liegt vor dem Live-Datum -> der Cron konnte noch nicht
    # feuern. Kein Alarm, nur Info ("pending").
    if wf.get("since"):
        live_at = parse_ts(wf["since"])
        if expected_at < live_at:
            return {"name": wf["name"], "status": "pending",
                    "detail": f"live seit {wf['since'][:16]}Z; erwarteter Termin {expected_at.isoformat()} davor - erster Cron steht noch aus",
                    "expected": expected_at.isoformat()}

    # als datetime, NICHT isoformat() - String-Vergleich gegen parse_ts()
    # war der Erst-Lauf-Bug (TypeError datetime >= str, 22.10.2026)
    since = expected_at - timedelta(hours=GRACE_HOURS)

    try:
        data = api(f"/repos/{REPO}/actions/workflows/{wf['file']}/runs",
                   params={"event": "schedule", "per_page": 30})
    except Exception as e:   # ApiError, URLError und alles andere: nie rot
        return {"name": wf["name"], "status": "error",
                "detail": f"{type(e).__name__}: {str(e)[:150]}",
                "expected": expected_at.isoformat()}

    runs = [r for r in data.get("workflow_runs", [])
            if parse_ts(r["created_at"]) >= since]
    if not runs:
        # v4: kein scheduled Run - manuellen Catch-up pruefen, bevor
        # "missing" alarmiert wird. Ein erfolgreicher Run JEDES Events
        # (z.B. workflow_dispatch nach ausgebliebenem Cron) erfuellt
        # den Zweck des Workflows und zaehlt als "ok (manuell)".
        try:
            any_data = api(f"/repos/{REPO}/actions/workflows/{wf['file']}/runs",
                           params={"per_page": 30})
            any_ok = [r for r in any_data.get("workflow_runs", [])
                      if r["status"] == "completed" and r["conclusion"] == "success"
                      and parse_ts(r["created_at"]) >= expected_at]
            if any_ok:
                newest = max(any_ok, key=lambda r: parse_ts(r["created_at"]))
                return {"name": wf["name"], "status": "ok",
                        "detail": f"Erfolg (manuell, event={newest.get('event', '?')}) {newest['created_at']} (Run {newest['id']})",
                        "expected": expected_at.isoformat()}
        except Exception:
            pass  # Fallback-Query gescheitert -> regulaer "missing" melden
        return {"name": wf["name"], "status": "missing",
                "detail": "kein scheduled Run seit erwartetem Termin",
                "expected": expected_at.isoformat()}

    ok_runs = [r for r in runs
               if r["status"] == "completed" and r["conclusion"] == "success"]
    if ok_runs:
        newest = max(ok_runs, key=lambda r: parse_ts(r["created_at"]))
        newest_ts = parse_ts(newest["created_at"])
        # v4: Verspaetung gegen den erwarteten Termin messen. Ueber der
        # Schwelle -> "late" (alarmpflichtig), darunter -> "ok" mit Angabe.
        delay = newest_ts - expected_at
        delay_h = delay.total_seconds() / 3600
        if delay_h > LATE_ALARM_HOURS:
            return {"name": wf["name"], "status": "late",
                    "detail": f"Erfolg {newest['created_at']} (Run {newest['id']}), aber {delay_h:.1f}h nach Termin {expected_at.isoformat()} (Schwelle {LATE_ALARM_HOURS}h)",
                    "expected": expected_at.isoformat()}
        return {"name": wf["name"], "status": "ok",
                "detail": f"Erfolg {newest['created_at']} (Run {newest['id']}), +{delay_h:.1f}h",
                "expected": expected_at.isoformat()}

    newest = max(runs, key=lambda r: parse_ts(r["created_at"]))
    conc = newest.get("conclusion") or newest.get("status")
    return {"name": wf["name"], "status": "failed",
            "detail": f"letzter Run {newest['created_at']}: {conc} (Run {newest['id']})",
            "expected": expected_at.isoformat()}


# ---- Issue-Verwaltung (Sammel-Issue, dedupliziert) --------------------

def ensure_label():
    try:
        api(f"/repos/{REPO}/labels/{LABEL}")
    except ApiError:
        try:
            api(f"/repos/{REPO}/labels", method="POST",
                body={"name": LABEL, "color": "d93f0b",
                      "description": "Actions-Waechter Alarm"})
        except ApiError:
            pass  # Label anlegen schlug fehl -> Issue ohne Label anlegen


def find_open_issue():
    try:
        issues = api(f"/repos/{REPO}/issues",
                     params={"labels": LABEL, "state": "open", "per_page": 5})
    except ApiError:
        return None
    for iss in issues:
        if "pull_request" not in iss:
            return iss
    return None


def issue_body(results):
    lines = ["**Actions-Waechter Alarm** - geplante Workflows nicht ok.",
             "",
             "| Workflow | Status | Detail | Erwartet (UTC) |",
             "|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['name']} | {r['status']} | {r['detail']} | {r['expected']} |")
    lines += ["", "Automatisch erzeugt von `.github/workflows/actions-waechter.yml`,",
              "dieses Issue wird vom Waechter aktualisiert und bei Erholung geschlossen."]
    return "\n".join(lines)


def manage_issue(results, problems, now):
    ensure_label()
    open_issue = find_open_issue()
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    if problems:
        body = issue_body(results)
        title = f"Actions-Waechter: {len(problems)} Workflow(s) nicht ok ({stamp})"
        if open_issue:
            api(f"/repos/{REPO}/issues/{open_issue['number']}",
                method="PATCH", body={"title": title, "body": body})
            print(f"Issue #{open_issue['number']} aktualisiert")
        else:
            new = api(f"/repos/{REPO}/issues", method="POST",
                      body={"title": title, "body": body, "labels": [LABEL]})
            print(f"Alarm-Issue #{new.get('number')} angelegt")
    elif open_issue:
        api(f"/repos/{REPO}/issues/{open_issue['number']}", method="PATCH",
            body={"state": "closed", "state_reason": "completed"})
        print(f"Issue #{open_issue['number']} geschlossen - alles ok")


def main():
    if not TOKEN:
        print("FEHLER: WAECHTER_TOKEN nicht gesetzt", file=sys.stderr)
        sys.exit(1)
    now = datetime.now(timezone.utc)

    results = [check_workflow(wf, now) for wf in EXPECTED]
    # pending = junge Workflows vor ihrem ersten Cron: KEIN Alarm-Grund.
    # v4: "late" alarmiert jetzt ebenfalls (Verspaetung ueber Schwelle).
    problems = [r for r in results if r["status"] not in ("ok", "pending")]

    status = {
        "generated_at": now.isoformat(),
        "ok": not problems,
        "grace_hours": GRACE_HOURS,
        "late_alarm_hours": LATE_ALARM_HOURS,
        "workflows": results,
    }
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
        f.write("\n")

    for r in results:
        print(f"[{r['status'].upper():7}] {r['name']}: {r['detail']}")
    if problems:
        print(f"{len(problems)} Problem(e) -> Issue-Alarm")

    try:
        manage_issue(results, problems, now)
    except Exception:
        # Alarmweg gescheitert: ROT machen, Traceback + API-Fehlerkoerper
        # stehen dann im Actions-Log (Status-File ist bereits geschrieben).
        traceback.print_exc()
        print("ALARM-WEG FEHLGESCHLAGEN - Watchdog konnte nicht alarmieren.",
              file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
