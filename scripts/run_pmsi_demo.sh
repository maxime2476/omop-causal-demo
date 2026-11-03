#!/usr/bin/env bash
# Phase 5 — génère la table de RUM FICTIVE puis l'ETL vers OMOP (schéma pmsi_omop) sur le
# PostgreSQL de la phase B (vocabulaire Athena déjà chargé dans CDM_SCHEMA).
# Usage (racine du dépôt) : bash scripts/run_pmsi_demo.sh
set -euo pipefail

cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo "Fichier .env absent : cp .env.example .env" >&2; exit 1; }
set -a; source .env; set +a

RUM_CSV="data/pmsi/rum_fictif.csv"
OUT_DIR="output/pmsi"
mkdir -p "$OUT_DIR"

uv run python pmsi_demo/generate_fake_rum.py --out "$RUM_CSV"

export PGPASSWORD="$POSTGRES_PASSWORD"
psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v cdm_schema="${CDM_SCHEMA:-cdm}" \
  -v rum_csv="$RUM_CSV" \
  -v out_mapping_rate="$OUT_DIR/mapping_rate.csv" \
  -v out_code_report="$OUT_DIR/code_report.csv" \
  -v out_table_counts="$OUT_DIR/table_counts.csv" \
  -v out_ref_concepts="$OUT_DIR/ref_concepts.csv" \
  -v out_vocab_coverage="$OUT_DIR/vocab_coverage.csv" \
  -f pmsi_demo/etl_rum_to_omop.sql | tee "$OUT_DIR/etl.log"
