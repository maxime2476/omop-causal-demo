# Phase B — ETL Synthea (CSV) -> OMOP CDM 5.4 sur PostgreSQL, avec ETLSyntheaBuilder.
#
# Usage (racine du dépôt, après `docker compose up -d` et `bash scripts/generate_synthea.sh`) :
#   Rscript r/02_synthea_etl_postgres.R          # ETL complet puis vérifications
#   Rscript r/02_synthea_etl_postgres.R verify   # vérifications seules
#
# Ordre des étapes : extras/codeToRun.R du dépôt OHDSI/ETL-Synthea. Signatures vérifiées dans la
# doc installée (ETLSyntheaBuilder 2.1, DatabaseConnector 8.0.0).
# Variables lues dans .env : POSTGRES_*, CDM_SCHEMA, SYNTHEA_SCHEMA, DATABASECONNECTOR_JAR_FOLDER,
# VOCAB_DIR (CSV Athena décompressés), VOCAB_BULK_LOAD (TRUE nécessite psql + POSTGRES_PATH).

SYNTHEA_VERSION <- "3.3.0"   # doit correspondre au JAR de scripts/generate_synthea.sh
CDM_VERSION <- "5.4"
SYNTHEA_CSV_DIR <- file.path("data", "synthea", "output", "csv")
OUTPUT_DIR <- file.path("output", "etl")

log_msg <- function(...) message(format(Sys.time(), "%H:%M:%S"), " ", sprintf(...))

read_settings <- function() {
  if (!file.exists(".env")) stop("Fichier .env absent : cp .env.example .env puis le compléter.")
  readRenviron(".env")
  get <- function(name, default = NULL) {
    value <- Sys.getenv(name, unset = "")
    if (nzchar(value)) return(value)
    if (is.null(default)) stop(sprintf("Variable %s manquante dans .env", name))
    default
  }
  list(
    host = get("POSTGRES_HOST", "localhost"),
    port = as.integer(get("POSTGRES_PORT", "5432")),
    db = get("POSTGRES_DB"),
    user = get("POSTGRES_USER"),
    password = get("POSTGRES_PASSWORD"),
    cdm_schema = get("CDM_SCHEMA", "cdm"),
    synthea_schema = get("SYNTHEA_SCHEMA", "native"),
    jar_folder = normalizePath(get("DATABASECONNECTOR_JAR_FOLDER", "data/jdbc"), mustWork = FALSE),
    vocab_dir = normalizePath(get("VOCAB_DIR", "data/vocab"), mustWork = FALSE),
    vocab_bulk = toupper(get("VOCAB_BULK_LOAD", "FALSE")) == "TRUE"
  )
}

ensure_jdbc_driver <- function(jar_folder) {
  if (length(list.files(jar_folder, pattern = "postgresql.*\\.jar$")) == 0) {
    dir.create(jar_folder, recursive = TRUE, showWarnings = FALSE)
    DatabaseConnector::downloadJdbcDrivers("postgresql", pathToDriver = jar_folder)
  }
}

check_inputs <- function(s) {
  if (!file.exists(file.path(SYNTHEA_CSV_DIR, "patients.csv"))) {
    stop("CSV Synthea absents : lancer bash scripts/generate_synthea.sh")
  }
  if (!file.exists(file.path(s$vocab_dir, "CONCEPT.csv"))) {
    stop(sprintf("Vocabulaires Athena absents : CONCEPT.csv introuvable dans %s", s$vocab_dir))
  }
}

connection_details <- function(s) {
  DatabaseConnector::createConnectionDetails(
    dbms = "postgresql",
    server = sprintf("%s/%s", s$host, s$db),
    port = s$port,
    user = s$user,
    password = s$password,
    pathToDriver = s$jar_folder
  )
}

timed <- function(label, expr) {
  log_msg("Début : %s", label)
  t0 <- Sys.time()
  force(expr)
  log_msg("Fin : %s (%.1f min)", label, as.numeric(difftime(Sys.time(), t0, units = "mins")))
}

run_etl <- function(cd, s) {
  timed("CreateCDMTables", ETLSyntheaBuilder::CreateCDMTables(
    connectionDetails = cd, cdmSchema = s$cdm_schema, cdmVersion = CDM_VERSION))
  timed("CreateSyntheaTables", ETLSyntheaBuilder::CreateSyntheaTables(
    connectionDetails = cd, syntheaSchema = s$synthea_schema, syntheaVersion = SYNTHEA_VERSION))
  timed("LoadSyntheaTables", ETLSyntheaBuilder::LoadSyntheaTables(
    connectionDetails = cd, syntheaSchema = s$synthea_schema, syntheaFileLoc = SYNTHEA_CSV_DIR))
  timed("LoadVocabFromCsv", ETLSyntheaBuilder::LoadVocabFromCsv(
    connectionDetails = cd, cdmSchema = s$cdm_schema, vocabFileLoc = s$vocab_dir,
    bulkLoad = s$vocab_bulk, delimiter = "\t"))
  timed("CreateMapAndRollupTables", ETLSyntheaBuilder::CreateMapAndRollupTables(
    connectionDetails = cd, cdmSchema = s$cdm_schema, syntheaSchema = s$synthea_schema,
    cdmVersion = CDM_VERSION, syntheaVersion = SYNTHEA_VERSION))
  timed("CreateExtraIndices", ETLSyntheaBuilder::CreateExtraIndices(
    connectionDetails = cd, cdmSchema = s$cdm_schema, syntheaSchema = s$synthea_schema,
    syntheaVersion = SYNTHEA_VERSION))
  timed("LoadEventTables", ETLSyntheaBuilder::LoadEventTables(
    connectionDetails = cd, cdmSchema = s$cdm_schema, syntheaSchema = s$synthea_schema,
    cdmVersion = CDM_VERSION, syntheaVersion = SYNTHEA_VERSION))
}

verify <- function(cd, s) {
  con <- DatabaseConnector::connect(cd)
  on.exit(DatabaseConnector::disconnect(con))
  tables <- DatabaseConnector::getTableNames(con, databaseSchema = s$cdm_schema)
  counts <- do.call(rbind, lapply(sort(tolower(tables)), function(tbl) {
    n <- DatabaseConnector::querySql(con, sprintf("SELECT COUNT(*) AS n FROM %s.%s",
                                                  s$cdm_schema, tbl))[[1]]
    log_msg("%-28s %12s lignes", tbl, format(n, big.mark = " "))
    data.frame(table_name = tbl, n_rows = n)
  }))
  dir.create(OUTPUT_DIR, recursive = TRUE, showWarnings = FALSE)
  write.csv(counts, file.path(OUTPUT_DIR, "row_counts.csv"), row.names = FALSE)

  n_patients <- nrow(data.table::fread(file.path(SYNTHEA_CSV_DIR, "patients.csv"),
                                       select = 1L))
  vocab <- vocab_load_check(counts, s$vocab_dir)
  write.csv(vocab, file.path(OUTPUT_DIR, "vocab_load_check.csv"), row.names = FALSE)
  n_person <- counts$n_rows[counts$table_name == "person"]
  cdm_source <- DatabaseConnector::querySql(con, sprintf("SELECT * FROM %s.cdm_source",
                                                         s$cdm_schema))
  write.csv(cdm_source, file.path(OUTPUT_DIR, "cdm_source.csv"), row.names = FALSE)
  checks <- data.frame(
    check = c("person == patients Synthea", "cdm_source renseignée",
              "vocabulaires : lignes en base == lignes CSV"),
    expected = c(n_patients, ">= 1 ligne", "toutes les tables"),
    observed = c(n_person, nrow(cdm_source), sprintf("%d/%d", sum(vocab$match), nrow(vocab))),
    passed = c(n_person == n_patients, nrow(cdm_source) >= 1, all(vocab$match))
  )
  write.csv(checks, file.path(OUTPUT_DIR, "acceptance.csv"), row.names = FALSE)
  print(checks)
  if (!all(checks$passed)) stop("Critères d'acceptation non remplis (voir output/etl/acceptance.csv)")
  log_msg("Critères d'acceptation remplis.")
}

vocab_load_check <- function(counts, vocab_dir) {
  # Compare les lignes chargées aux lignes des CSV Athena (hors en-tête), comptées en
  # séparateurs de ligne : détecte des lignes perdues ou fusionnées par le parsing des guillemets.
  files <- c("concept", "concept_ancestor", "concept_class", "concept_relationship",
             "concept_synonym", "domain", "drug_strength", "relationship", "vocabulary")
  do.call(rbind, lapply(files, function(tbl) {
    csv <- file.path(vocab_dir, paste0(toupper(tbl), ".csv"))
    n_csv <- as.numeric(system2("wc", c("-l", shQuote(csv)), stdout = TRUE) |>
                          strsplit(" ") |> unlist() |> head(1)) - 1
    n_db <- counts$n_rows[counts$table_name == tbl]
    data.frame(table_name = tbl, n_csv = n_csv, n_db = n_db, match = n_csv == n_db)
  }))
}

main <- function() {
  verify_only <- identical(commandArgs(trailingOnly = TRUE), "verify")
  s <- read_settings()
  check_inputs(s)
  ensure_jdbc_driver(s$jar_folder)
  cd <- connection_details(s)
  if (!verify_only) run_etl(cd, s)
  verify(cd, s)
}

main()
