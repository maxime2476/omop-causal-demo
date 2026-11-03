# Phase A1 — Télécharge le jeu Eunomia GiBleed (OMOP-CDM, données simulées)
# et le charge dans un fichier DuckDB : data/gibleed.duckdb
#
# Usage (racine du dépôt) : Rscript r/01_eunomia_to_duckdb.R [cdmVersion]
# GiBleed n'est publié qu'en CDM 5.3 et 5.5 (OHDSI/EunomiaDatasets) ; 5.3 par défaut,
# format de référence supporté par CohortMethod (phase D).
# Signatures vérifiées dans la doc installée (Eunomia 2.2.0) :
#   downloadEunomiaData(datasetName, cdmVersion, pathToData, overwrite, verbose)
#   getDatabaseFile(datasetName, cdmVersion, pathToData, dbms, databaseFile, inputFormat, verbose, overwrite)

log_msg <- function(...) message(format(Sys.time(), "%H:%M:%S"), " ", sprintf(...))

parse_cdm_version <- function() {
  args <- commandArgs(trailingOnly = TRUE)
  if (length(args) >= 1) args[[1]] else "5.3"
}

download_gibleed <- function(data_folder, cdm_version) {
  dir.create(data_folder, recursive = TRUE, showWarnings = FALSE)
  Eunomia::downloadEunomiaData(
    datasetName = "GiBleed",
    cdmVersion = cdm_version,
    pathToData = data_folder,
    overwrite = FALSE,
    verbose = TRUE
  )
}

build_duckdb <- function(data_folder, cdm_version, db_file) {
  if (file.exists(db_file)) {
    log_msg("Remplacement de %s", db_file)
    unlink(db_file)
  }
  Eunomia::getDatabaseFile(
    datasetName = "GiBleed",
    cdmVersion = cdm_version,
    pathToData = data_folder,
    dbms = "duckdb",
    databaseFile = db_file,
    overwrite = TRUE,
    verbose = TRUE
  )
}

log_table_counts <- function(db_file) {
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_file, read_only = TRUE)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  for (tbl in sort(DBI::dbListTables(con))) {
    n <- DBI::dbGetQuery(con, sprintf('SELECT count(*) AS n FROM "%s"', tbl))$n
    log_msg("%-25s %10d lignes", tbl, as.integer(n))
  }
}

main <- function() {
  cdm_version <- parse_cdm_version()
  data_folder <- normalizePath(file.path("data", "eunomia"), mustWork = FALSE)
  db_file <- normalizePath(file.path("data", "gibleed.duckdb"), mustWork = FALSE)

  log_msg("GiBleed CDM %s -> %s", cdm_version, db_file)
  download_gibleed(data_folder, cdm_version)
  build_duckdb(data_folder, cdm_version, db_file)
  log_table_counts(db_file)
  log_msg("Terminé : %s", db_file)
}

main()
