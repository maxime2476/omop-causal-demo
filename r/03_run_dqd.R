# Phase C — Data Quality Dashboard (OHDSI) sur le CDM 5.4 PostgreSQL issu de la phase B.
#
# Usage (racine du dépôt) : Rscript r/03_run_dqd.R [numThreads, défaut 1]
# Sortie : output/dqd/dqd_results.json (+ CSV des résultats, journaux)
# Signature vérifiée dans la doc installée (DataQualityDashboard 2.9.1) : cdmVersion vaut
# "5.3" par défaut, d'où la valeur explicite ci-dessous.

CDM_VERSION <- "5.4"
OUTPUT_DIR <- file.path("output", "dqd")

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
    results_schema = get("RESULTS_SCHEMA", "results"),
    jar_folder = normalizePath(get("DATABASECONNECTOR_JAR_FOLDER", "data/jdbc"), mustWork = FALSE)
  )
}

main <- function() {
  args <- commandArgs(trailingOnly = TRUE)
  # 1 thread par défaut : avec numThreads > 1, DataQualityDashboard 2.9.1 remet sa variable
  # `connection` à NULL après avoir enregistré on.exit(disconnect(connection)) ; la déconnexion
  # finale échoue alors (« no applicable method for `@` applied to NULL »), une fois les
  # résultats déjà écrits.
  num_threads <- if (length(args) >= 1) as.integer(args[[1]]) else 1L
  s <- read_settings()
  cd <- DatabaseConnector::createConnectionDetails(
    dbms = "postgresql",
    server = sprintf("%s/%s", s$host, s$db),
    port = s$port,
    user = s$user,
    password = s$password,
    pathToDriver = s$jar_folder
  )
  dir.create(OUTPUT_DIR, recursive = TRUE, showWarnings = FALSE)
  log_msg("DQD sur %s.%s (CDM %s, %d threads)", s$db, s$cdm_schema, CDM_VERSION, num_threads)
  t0 <- Sys.time()
  DataQualityDashboard::executeDqChecks(
    connectionDetails = cd,
    cdmDatabaseSchema = s$cdm_schema,
    resultsDatabaseSchema = s$results_schema,
    vocabDatabaseSchema = s$cdm_schema,
    cdmSourceName = "Synthea synthetic (omop-causal-demo)",
    cdmVersion = CDM_VERSION,
    numThreads = num_threads,
    outputFolder = OUTPUT_DIR,
    outputFile = "dqd_results.json",
    writeToTable = TRUE,
    writeToCsv = TRUE,
    csvFile = "dqd_results.csv",   # relatif à outputFolder
    verboseMode = FALSE,
    # Tables de vocabulaire (exclues par défaut) + COHORT / COHORT_DEFINITION : le DQD les cherche
    # dans le schéma résultats alors que CreateCDMTables les crée (vides) dans le schéma CDM, ce qui
    # produit 46 erreurs d'exécution sans rapport avec la qualité des données.
    tablesToExclude = c("CONCEPT", "VOCABULARY", "CONCEPT_ANCESTOR", "CONCEPT_RELATIONSHIP",
                        "CONCEPT_CLASS", "CONCEPT_SYNONYM", "RELATIONSHIP", "DOMAIN",
                        "PACK_CONTENT", "CONCEPT_METADATA", "CONCEPT_RELATIONSHIP_METADATA",
                        "COHORT", "COHORT_DEFINITION")
  )
  log_msg("Terminé en %.1f min : %s", as.numeric(difftime(Sys.time(), t0, units = "mins")),
          file.path(OUTPUT_DIR, "dqd_results.json"))
}

main()
