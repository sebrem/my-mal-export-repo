# Abo-Discovery-Vorfilter – Commit-Paket (20.10.2026)

## Inhalt

| Datei | Ziel im Repo `sebrem/my-mal-export-repo` |
|---|---|
| `scripts/abo_discovery.py` | neu |
| `abo_snapshots/discovery_exclude.json` | neu |
| `workflow-step.md` | Ergänzung für `.github/workflows/abo-katalog-check.yml` |

## Was es tut

`abo_discovery.py` läuft im `abgleich`-Job der Montags-Action direkt nach `mal_aliases.py` und schreibt `abo_snapshots/discovery_candidates.json`:

- Sweep aller 4 Abo-Katalog-Snapshots (Manga UP!, Manga+, MangaPlaza Read-in-Full, JNC) nach Kernmuster-Keywords (Ehe-Prämisse, Gal×Introvert, Office, Yuri, Katzen, Kreaturen, Food/Slow-Life, Iyashikei, Kuudere/Isolation, Erwachsenen-Romance; JNC zusätzlich tag-basiert inkl. Party-Tsuihou)
- Ausschluss über `discovery_exclude.json` (bereits dokumentierte/gelesene Titel – vom Agenten bei jedem Fund nachpflegen)
- MAL-Abgleich über `mal_aliases.json` (EN/JP/Synonyme → MAL-ID) und `mal_manga.json` (Achtung: Feld heißt `id`, nicht `mal_id`) → Titel mit MAL-Status landen in `already_tracked` (kein Neufund, aber Status-Drift sichtbar)
- MangaPlaza-Harlequin-Rauschen (Billionaire/Sheikh/…) mit nur generischem Ehe-Treffer landet in `mp_noise`
- Reihenfolge der Kandidaten-Plattformen im Output: jnc → mangaplaza → mangaplus → mangaup (Vorlese-Quote bei Manga UP! ist hoch)

Der monatliche Abo-Discovery-Check (20.) liest dann nur noch `discovery_candidates.json` und verifiziert die Kandidaten (Doppelkauf-Check + MAL-Lesestand) statt selbst zu sweepten. Fund ≠ Empfehlung bleibt Regel.

## Einbau

1. Beide neuen Dateien ins Repo committen (Pfade wie oben).
2. In `.github/workflows/abo-katalog-check.yml` im Job `abgleich` nach dem Step „MAL-Aliase + offene Titel matchen" den Step aus `workflow-step.md` einfügen (der Commit-Step am Ende fasst `abo_snapshots/` bereits zusammen, keine weitere Änderung nötig).
3. Fertig – der nächste Montags-Lauf erzeugt die Kandidatenliste; danach einmalig den Abo-Discovery-Task-Prompt um die Zeile ergänzen: „Sweep: discovery_candidates.json im Repo lesen statt Roh-Sweep."

## Ausschluss-Liste pflegen

`discovery_exclude.json` ist der Verhinderer von Doppelmeldungen. Jeder im Backlog dokumentierte Abo-Fund und jeder bekannte Lesestand gehört als lowercase-Fragment hinein (Substring-Match reicht). Aktuell vorbefüllt mit allen Funden vom 18./20.10.2026 und den im MAL-Export 19.10.2026 sichtbaren Leseständen.
