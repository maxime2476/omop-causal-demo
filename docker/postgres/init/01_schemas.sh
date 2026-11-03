#!/usr/bin/env bash
# Exécuté une seule fois, à la création du volume : schémas CDM, Synthea natif et résultats.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE SCHEMA IF NOT EXISTS ${CDM_SCHEMA};
CREATE SCHEMA IF NOT EXISTS ${SYNTHEA_SCHEMA};
CREATE SCHEMA IF NOT EXISTS ${RESULTS_SCHEMA};
SQL
