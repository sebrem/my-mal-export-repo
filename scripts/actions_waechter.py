#!/usr/bin/env python3
"""Actions-Waechter (stdlib only).

Taeglicher Watchdog ueber alle geplanten Workflows des Repos. Pro erwartetem
Workflow wird geprueft, ob seit dessen letztem erwarteten Cron-Termin ein
erfolgreicher scheduled Run existiert. Ergebnisse:
  - ok      : Erfolg-Run seit letztem erwarteten Termin vorhanden
  - failed  : Run(s) vorhanden, aber kein Erfolg (conclusion != success)
  - missing : gar kein scheduled Run seit letztem erwarteten Termin
  - error   : Workflow/API nicht abrufbar (Starke-Metrik, im Issue genannt)

Alarmweg: GitHub-Issue mit Label "actions-waechter" (ein offenes
Sammel-Issue, aktualisiert statt vermehrt; wird bei Erholung geschlossen).
Zusatz: actions_status.json als Heartbeat-/Status-Datei (Commit macht der
Workflow, nicht das Skript).

Jitter-Behandlung: Ein Run zaehlt als "puenktlich", wenn er ab
(termin - 4h GRACE) gestartet ist. Verspaetungen bis zur Watchdog-Laufzeit
sind damit unkritisch; laeuft eine Action erst NACH dem Watchdog, erscheint
sie spaetestens beim naechsten Watchdog-Lauf als ok (kein Fehlalarm, nur
eine Verzoegerung von max. 24h bei der Entdeckung).
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

REPO = os.environ.get("WAECHTER_REPO", "sebrem/my-mal-export-repo")
TOKEN = os.environ.get("WAECHTER_TOKEN", "")
LABEL = "actions-waechter"
GRACE_HOURS = 4
STATUS_FILE = "actions_status.json"

# Erwartete scheduled Workflows (Wochentage: Mo=0 .. So=6, UTC).
# Pfad relativ zu .github/workflows/. Bei neuen/verschobenen Crons hier
# nachpflegen - single source of truth fuer den Watchdog.
EXPECTED = [
    {"file": "mal-export.yml",            "name": "MAL Sync",               "days": [0], "hour": 4, "minute": 17},
    {"file": "mal_neighbours.yml",       "name": "MAL Friends Candidates",  "days": [0], "hour": 4, "minute": 35},
    {"file": "anilist_export.yml",       "name": "AniList Export",          "days": [0], "hour": 4, "minute": 47},
    {"file": "abo-katalog-check.yml",     "name": "Abo-Katalog-Check",       "days": [0], "hour": 5, "minute": 17},
    {"file": "verlags-announcements.yml", "name": "Verlags-Announcements",  "days": [2], "hour": 5, "minute": 47},
    {"file": "release-kalender.yml",     "name": "Release-Kalender",        "days": [3], "hour": 4, "minute": 30},
]


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
    with urllib.request.urlopen(req) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else {}


def last_expected_trigger(now, days, hour, minute):
    """Letzter erwarteter Cron-Termin <= now (sucht bis 7 Tage zurueck)."""
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
    expected_at = last_expected_trigger(now, wf["days"], wf["hour"], wf["minute"])
    if expected_at is None:
        return {"name": wf["name"], "status": "error",
                "detail": "kein erwarteter Termin berechenbar (Konfig?)",
                "expected": None}
    since = (expected_at - timedelta(hours=GRACE_HOURS)).isoformat()

    try:
        data = api(f"/repos/{REPO}/actions/workflows/{wf['file']}/runs",
                   params={"event": "schedule", "per_page": 30})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"name": wf["name"], "status": "error",
                    "detail": f"Workflow-Datei {wf['file']} nicht gefunden",
                    "expected": expected_at.isoformat()}
        return {"name": wf["name"], "status": "error",
                "detail": f"API-Fehler {e.code}", "expected": expected_at.isoformat()}

    runs = [r for r in data.get("workflow_runs", [])
            if parse_ts(r["created_at"]) >= since]
    if not runs:
        return {"name": wf["name"], "status": "missing",
                "detail": "kein scheduled Run seit erwartetem Termin",
                "expected": expected_at.isoformat()}

    ok_runs = [r for r in runs
               if r["status"] == "completed" and r["conclusion"] == "success"]
    if ok_runs:
        newest = max(ok_runs, key=lambda r: parse_ts(r["created_at"]))
        return {"name": wf["name"], "status": "ok",
                "detail": f"Erfolg {newest['created_at']} (Run {newest['id']})",
                "expected": expected_at.isoformat()}

    newest = max(runs, key=lambda r: parse_ts(r["created_at"]))
    conc = newest.get("conclusion") or newest.get("status")
    return {"name": wf["name"], "status": "failed",
            "detail": f"letzter Run {newest['created_at']}: {conc} (Run {newest['id']})",
            "expected": expected_at.isoformat()}


# ---- Issue-Verwaltung (Sammel-Issue, dedupliziert) --------------------

def ensure_label():
    try:
        api(f"/repos/{REPO}/labels")
    except urllib.error.HTTPError:
        pass
    try:
        api(f"/repos/{REPO}/labels/{LABEL}")
    except urllib.error.HTTPError:
        try:
            api(f"/repos/{REPO}/labels", method="POST",
                body={"name": LABEL, "color": "d93f0b",
                      "description": "Actions-Waechter Alarm"})
        except urllib.error.HTTPError:
            pass  # Label anlegen schlug fehl -> Issue ohne Label anlegen


def find_open_issue():
    try:
        issues = api(f"/repos/{REPO}/issues",
                    params={"labels": LABEL, "state": "open", "per_page": 5})
    except urllib.error.HTTPError:
        return None
    for iss in issues:
        if "pull_request" not in iss:
            return iss
    return None


def issue_body(results, problems):
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
        body = issue_body(results, problems)
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
    problems = [r for r in results if r["status"] != "ok"]

    status = {
        "generated_at": now.isoformat(),
        "ok": not problems,
        "grace_hours": GRACE_HOURS,
        "workflows": results,
    }
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
        f.write("\n")

    for r in results:
        print(f"[{r['status'].upper():7}] {r['name']}: {r['detail']}")
    if problems:
        print(f"{len(problems)} Problem(e) -> Issue-Alarm")
    else:
        print("Alle erwarteten Workflows ok.")

    manage_issue(results, problems, now)
    sys.exit(0)  # Alarmweg ist das Issue; der Workflow-Run bleibt gruen


if __name__ == "__main__":
    main()
