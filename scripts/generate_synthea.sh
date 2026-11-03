#!/usr/bin/env bash
# Phase B — Génère une population Synthea reproductible, export CSV uniquement.
# Usage (racine du dépôt) : bash scripts/generate_synthea.sh [population]
#
# - Synthea v3.3.0 : dernière version acceptée par ETL-Synthea (CreateSyntheaTables).
# - Graines patients/cliniciens et dates de référence et de fin fixées : sortie reproductible
#   (sans -e, les rencontres en cours sont closes à l'heure d'exécution).
# - -o false : pas de population de remplacement des décès, donc exactement N patients générés.
set -euo pipefail

SYNTHEA_VERSION="3.3.0"
POPULATION="${1:-1000}"
SEED=42
REFERENCE_DATE=20251231   # date de référence et date de fin de simulation
JAR="data/synthea/bin/synthea-with-dependencies.jar"
OUT_DIR="data/synthea/output"

if [[ ! -f "$JAR" ]]; then
  echo "JAR absent : gh release download v${SYNTHEA_VERSION} -R synthetichealth/synthea \\" >&2
  echo "  -p synthea-with-dependencies.jar -D data/synthea/bin" >&2
  exit 1
fi

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

java -jar "$JAR" \
  -s "$SEED" -cs "$SEED" -r "$REFERENCE_DATE" -e "$REFERENCE_DATE" -p "$POPULATION" -o false \
  --exporter.baseDirectory="$OUT_DIR" \
  --exporter.csv.export=true \
  --exporter.fhir.export=false \
  --exporter.fhir.transaction_bundle=false \
  --exporter.hospital.fhir.export=false \
  --exporter.practitioner.fhir.export=false \
  --exporter.csv.append_mode=false \
  Massachusetts

echo "Patients générés : $(($(wc -l < "$OUT_DIR/csv/patients.csv") - 1))"
