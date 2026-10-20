# Workflow-Step für .github/workflows/abo-katalog-check.yml
# Einfügen im Job "abgleich", NACH dem Step "MAL-Aliase + offene Titel matchen",
# VOR dem Step "Änderungen committen".

      # Abo-Discovery-Vorfilter: Kernmuster-Kandidaten aus allen 4
      # Katalog-Snapshots + MAL-Tracking-Check -> discovery_candidates.json
      # (Verifikation bleibt dem monatlichen Abo-Discovery-Check überlassen)
      - name: Abo-Discovery-Kandidaten (Vorfilter)
        run: python scripts/abo_discovery.py
        env:
          REPO_ROOT: ${{ github.workspace }}
        continue-on-error: true
