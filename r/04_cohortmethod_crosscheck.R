# Phase D — Contre-vérification de l'analyse A2 avec le package OHDSI CohortMethod.
#
# Usage (racine du dépôt, après `uv run omop-causal study`) :
#   Rscript r/04_cohortmethod_crosscheck.R
#
# Principe : CohortMethod reçoit EXACTEMENT la cohorte éligible de l'implémentation Python
# (output/report/cohort.csv : mêmes personnes, même t0). Les écarts de HR viennent donc de la
# méthode (covariables FeatureExtraction, PS LASSO Cyclops, modèle de Cox), pas de la population.
# Le critère est reconstruit ici par la même règle que sql/cohorts/outcome.sql (descendants du
# concept via CONCEPT_ANCESTOR).
#
# Signatures vérifiées dans la doc installée (CohortMethod 6.0.3, FeatureExtraction 3.14.0).
# La base d'origine n'est jamais modifiée : la table de cohortes est écrite dans une copie,
# data/gibleed_cm.duckdb, recréée à chaque exécution.

SOURCE_DB <- file.path("data", "gibleed.duckdb")
CM_DB <- file.path("data", "gibleed_cm.duckdb")
PY_DIR <- file.path("output", "report")
OUTPUT_DIR <- file.path("output", "cohortmethod")
CDM_SCHEMA <- "main"
COHORT_TABLE <- "cm_cohort"
TARGET_ID <- 1L
COMPARATOR_ID <- 2L
OUTCOME_ID <- 3L
ITT_DAYS <- 365L       # config/study.yaml : follow_up.itt_days
CALIPER_SD <- 0.2      # config/study.yaml : propensity.caliper_sd

log_msg <- function(...) message(format(Sys.time(), "%H:%M:%S"), " ", sprintf(...))

check_inputs <- function() {
  for (f in c(SOURCE_DB, file.path(PY_DIR, "cohort.csv"), file.path(PY_DIR, "concepts.csv"),
              file.path(PY_DIR, "results.csv"))) {
    if (!file.exists(f)) stop(sprintf("%s absent : lancer d'abord `uv run omop-causal study`", f))
  }
}

# Vérifie par requête que les concept_id de l'étude Python sont toujours ceux de CONCEPT
# (nom + vocabulaire + classe, concept standard et valide).
verify_concepts <- function(con, concepts) {
  ids <- vapply(seq_len(nrow(concepts)), function(i) {
    row <- concepts[i, ]
    found <- DBI::dbGetQuery(con, "
      SELECT concept_id FROM concept
      WHERE lower(concept_name) = lower(?) AND vocabulary_id = ? AND concept_class_id = ?
        AND standard_concept = 'S' AND invalid_reason IS NULL",
      params = list(row$concept_name, row$vocabulary_id, row$concept_class_id))$concept_id
    if (length(found) != 1 || found != row$concept_id) {
      stop(sprintf("Concept %s : %s trouvé, %s attendu", row$label,
                   paste(found, collapse = ","), row$concept_id))
    }
    log_msg("Concept %-14s -> %d (%s)", row$label, found, row$concept_name)
    as.integer(found)
  }, integer(1))
  stats::setNames(ids, concepts$label)
}

build_cohort_table <- function(con, cohort, outcome_concept_id) {
  exposures <- data.frame(
    cohort_definition_id = ifelse(cohort$arm == "target", TARGET_ID, COMPARATOR_ID),
    subject_id = as.numeric(cohort$person_id),
    cohort_start_date = as.Date(cohort$t0),
    cohort_end_date = as.Date(cohort$exposure_end_date)
  )
  DBI::dbExecute(con, sprintf("DROP TABLE IF EXISTS %s", COHORT_TABLE))
  DBI::dbWriteTable(con, COHORT_TABLE, exposures)
  # Critère : toutes les occurrences (descendants inclus), comme sql/cohorts/outcome.sql.
  DBI::dbExecute(con, sprintf("
    INSERT INTO %s
    SELECT DISTINCT %d, co.person_id, co.condition_start_date, co.condition_start_date
    FROM condition_occurrence co
    JOIN concept_ancestor ca ON ca.descendant_concept_id = co.condition_concept_id
    WHERE ca.ancestor_concept_id = %d", COHORT_TABLE, OUTCOME_ID, outcome_concept_id))
  counts <- DBI::dbGetQuery(con, sprintf(
    "SELECT cohort_definition_id, COUNT(*) AS n FROM %s GROUP BY 1 ORDER BY 1", COHORT_TABLE))
  for (i in seq_len(nrow(counts))) {
    log_msg("Cohorte %d : %d lignes", counts$cohort_definition_id[i], counts$n[i])
  }
}

# Effectifs et événements par bras, lus directement dans la population analysée.
population_counts <- function(pop, weighted) {
  pop <- as.data.frame(pop)
  ev <- pop$outcomeCount > 0
  data.frame(
    n_target = sum(pop$treatment == 1), n_comparator = sum(pop$treatment == 0),
    events_target = sum(ev & pop$treatment == 1),
    events_comparator = sum(ev & pop$treatment == 0),
    max_weight = if (weighted) max(pop$iptw) else NA_real_
  )
}

estimate <- function(label, pop, args) {
  log_msg("Modèle de Cox : %s", label)
  om <- CohortMethod::fitOutcomeModel(population = pop, fitOutcomeModelArgs = args)
  est <- om$outcomeModelTreatmentEstimate
  cbind(
    data.frame(analysis = label, status = om$outcomeModelStatus,
               hr = exp(est$logRr), ci_low = exp(est$logLb95), ci_high = exp(est$logUb95),
               se_log_hr = est$seLogRr),
    population_counts(pop, weighted = isTRUE(args$inversePtWeighting))
  )
}

balance_summary <- function(label, balance) {
  balance <- as.data.frame(balance)
  before <- grep("^before.*StdDiff$", names(balance), value = TRUE)[1]
  after <- grep("^after.*StdDiff$", names(balance), value = TRUE)[1]
  data.frame(
    sample = label, n_covariates = nrow(balance),
    max_abs_smd_before = max(abs(balance[[before]]), na.rm = TRUE),
    max_abs_smd_after = max(abs(balance[[after]]), na.rm = TRUE),
    n_smd_after_ge_0.1 = sum(abs(balance[[after]]) >= 0.1, na.rm = TRUE)
  )
}

compare_with_python <- function(cm) {
  py <- utils::read.csv(file.path(PY_DIR, "results.csv"))
  py <- py[py$follow_up == "itt", ]
  trunc <- utils::read.csv(file.path(PY_DIR, "sensitivity_truncation.csv"))
  untrunc <- trunc[trunc$truncation == "0-100", ]
  pairs <- list(
    c("unadjusted", "unadjusted", "Non ajusté"),
    c("matched_unstratified", "matched", "Appariement PS 1:1, Cox non stratifié"),
    c("matched_stratified", "matched", "Appariement PS 1:1, Cox stratifié sur la paire (CM)"),
    c("iptw_ate", "iptw_ate_untruncated", "IPTW ATE sans troncature"),
    c("iptw_ate", "iptw_ate", "IPTW ATE (Python tronqué 1-99 %)")
  )
  do.call(rbind, lapply(pairs, function(p) {
    c_row <- cm[cm$analysis == p[1], ]
    p_row <- if (p[2] == "iptw_ate_untruncated") untrunc else py[py$method == p[2], ]
    data.frame(
      comparison = p[3], cm_analysis = p[1], python_analysis = p[2],
      python_n = p_row$n_target + p_row$n_comparator, cm_n = c_row$n_target + c_row$n_comparator,
      python_events = p_row$events_target + p_row$events_comparator,
      cm_events = c_row$events_target + c_row$events_comparator,
      python_hr = p_row$hr, python_ci_low = p_row$ci_low, python_ci_high = p_row$ci_high,
      cm_hr = c_row$hr, cm_ci_low = c_row$ci_low, cm_ci_high = c_row$ci_high,
      ratio_hr_cm_python = c_row$hr / p_row$hr
    )
  }))
}

main <- function() {
  check_inputs()
  dir.create(OUTPUT_DIR, recursive = TRUE, showWarnings = FALSE)
  if (!file.copy(SOURCE_DB, CM_DB, overwrite = TRUE)) stop("Copie de la base impossible")
  cohort <- utils::read.csv(file.path(PY_DIR, "cohort.csv"))
  concepts <- utils::read.csv(file.path(PY_DIR, "concepts.csv"))
  log_msg("Cohorte Python : %d personnes (%d cible, %d comparateur)", nrow(cohort),
          sum(cohort$arm == "target"), sum(cohort$arm == "comparator"))

  con <- DBI::dbConnect(duckdb::duckdb(), CM_DB)
  ids <- verify_concepts(con, concepts)
  build_cohort_table(con, cohort, ids[["GI hemorrhage"]])
  DBI::dbDisconnect(con, shutdown = TRUE)

  cd <- DatabaseConnector::createConnectionDetails(dbms = "duckdb", server = CM_DB)
  # Covariables FeatureExtraction par défaut, sans les deux molécules comparées (ni descendants).
  covariate_settings <- FeatureExtraction::createDefaultCovariateSettings(
    excludedCovariateConceptIds = c(ids[["celecoxib"]], ids[["diclofenac"]]),
    addDescendantsToExclude = TRUE
  )
  log_msg("Extraction CohortMethodData (cohortes déjà éligibles : aucun filtre supplémentaire)")
  cm_data <- CohortMethod::getDbCohortMethodData(
    connectionDetails = cd, cdmDatabaseSchema = CDM_SCHEMA,
    targetId = TARGET_ID, comparatorId = COMPARATOR_ID, outcomeIds = OUTCOME_ID,
    exposureDatabaseSchema = CDM_SCHEMA, exposureTable = COHORT_TABLE,
    outcomeDatabaseSchema = CDM_SCHEMA, outcomeTable = COHORT_TABLE,
    getDbCohortMethodDataArgs = CohortMethod::createGetDbCohortMethodDataArgs(
      removeDuplicateSubjects = "keep all", firstExposureOnly = FALSE, washoutPeriod = 0,
      restrictToCommonPeriod = FALSE, covariateSettings = covariate_settings)
  )
  n_covariates <- nrow(dplyr::collect(cm_data$covariateRef))

  # Fenêtre ITT : jours 1 à 365 après t0, censure à la fin d'observation (comme le Python, où le
  # critère doit être strictement postérieur à t0 et le temps se compte depuis t0).
  pop <- CohortMethod::createStudyPopulation(
    cohortMethodData = cm_data, outcomeId = OUTCOME_ID,
    createStudyPopulationArgs = CohortMethod::createCreateStudyPopulationArgs(
      removeSubjectsWithPriorOutcome = TRUE, priorOutcomeLookback = 99999, minDaysAtRisk = 1,
      riskWindowStart = 1, startAnchor = "cohort start",
      riskWindowEnd = ITT_DAYS, endAnchor = "cohort start")
  )
  attrition <- CohortMethod::getAttritionTable(pop)

  log_msg("Score de propension (LASSO Cyclops, validation croisée) sur %d covariables",
          n_covariates)
  ps <- CohortMethod::createPs(
    cohortMethodData = cm_data, population = pop,
    createPsArgs = CohortMethod::createCreatePsArgs(estimator = "ate")
  )
  ps_model <- as.data.frame(CohortMethod::getPsModel(ps, cm_data))
  auc <- CohortMethod::computePsAuc(ps)
  equipoise <- CohortMethod::computeEquipoise(ps)

  matched <- CohortMethod::matchOnPs(
    population = ps,
    matchOnPsArgs = CohortMethod::createMatchOnPsArgs(
      caliper = CALIPER_SD, caliperScale = "standardized logit", maxRatio = 1)
  )
  cox <- function(...) CohortMethod::createFitOutcomeModelArgs(modelType = "cox", ...)
  results <- rbind(
    estimate("unadjusted", pop, cox()),
    estimate("matched_unstratified", matched, cox(stratified = FALSE)),
    estimate("matched_stratified", matched, cox(stratified = TRUE)),
    # IC par bootstrap : l'IC par profil de vraisemblance ignore la pondération.
    estimate("iptw_ate", ps, cox(inversePtWeighting = TRUE, bootstrapCi = TRUE))
  )

  log_msg("Équilibre des covariables")
  balance_matched <- CohortMethod::computeCovariateBalance(matched, cm_data)
  balance_iptw <- CohortMethod::computeCovariateBalance(ps, cm_data)
  balance <- rbind(balance_summary("matched", balance_matched),
                   balance_summary("iptw_ate", balance_iptw))

  comparison <- compare_with_python(results)
  summary <- data.frame(
    item = c("n_covariates", "n_covariates_in_ps_model", "ps_auc", "equipoise_0.3_0.7",
             "n_matched_pairs"),
    value = c(n_covariates, sum(ps_model$coefficient != 0 & ps_model$covariateId != 0,
                                 na.rm = TRUE),
              auc, equipoise, sum(matched$treatment == 1))
  )

  write.csv(attrition, file.path(OUTPUT_DIR, "attrition.csv"), row.names = FALSE)
  write.csv(results, file.path(OUTPUT_DIR, "results.csv"), row.names = FALSE)
  write.csv(balance, file.path(OUTPUT_DIR, "balance_summary.csv"), row.names = FALSE)
  write.csv(ps_model, file.path(OUTPUT_DIR, "ps_model.csv"), row.names = FALSE)
  write.csv(summary, file.path(OUTPUT_DIR, "summary.csv"), row.names = FALSE)
  write.csv(comparison, file.path(OUTPUT_DIR, "comparison.csv"), row.names = FALSE)
  print(summary)
  print(results[, c("analysis", "n_target", "n_comparator", "hr", "ci_low", "ci_high")])
  print(comparison[, c("comparison", "python_hr", "cm_hr", "ratio_hr_cm_python")])
  log_msg("Sorties écrites dans %s", OUTPUT_DIR)
}

main()
