-- Diagnostics post-ETL (phase B). Exécution :
--   docker exec -i omop-postgres psql -U omop -d omop --csv -q < sql/etl/diagnostics.sql
-- Schémas : native (Synthea brut), cdm (OMOP).

\echo '## conditions Synthea : domaine du concept standard cible'
SELECT coalesce(m.target_domain_id, '(non mappé)') AS target_domain, count(*) AS n
FROM native.conditions c
LEFT JOIN cdm.source_to_standard_vocab_map m
  ON m.source_code = c.code::text AND m.source_vocabulary_id = 'SNOMED'
GROUP BY 1 ORDER BY 2 DESC;

\echo '## rencontres Synthea par classe : rattachées à une visite OMOP'
SELECT e.encounterclass, count(*) AS n, sum((f.encounter_id IS NOT NULL)::int) AS rattachees
FROM native.encounters e
LEFT JOIN cdm.final_visit_ids f ON f.encounter_id = e.id
GROUP BY 1 ORDER BY 2 DESC;

\echo '## événements OMOP sans visit_occurrence_id'
SELECT 'condition_occurrence' AS table_name, count(*) AS n,
       sum((visit_occurrence_id IS NULL)::int) AS n_sans_visite FROM cdm.condition_occurrence
UNION ALL SELECT 'drug_exposure', count(*), sum((visit_occurrence_id IS NULL)::int) FROM cdm.drug_exposure
UNION ALL SELECT 'procedure_occurrence', count(*), sum((visit_occurrence_id IS NULL)::int) FROM cdm.procedure_occurrence
UNION ALL SELECT 'measurement', count(*), sum((visit_occurrence_id IS NULL)::int) FROM cdm.measurement
UNION ALL SELECT 'observation', count(*), sum((visit_occurrence_id IS NULL)::int) FROM cdm.observation;

\echo '## visit_occurrence par visit_concept_id'
SELECT v.visit_concept_id, c.concept_name, count(*) AS n
FROM cdm.visit_occurrence v JOIN cdm.concept c ON c.concept_id = v.visit_concept_id
GROUP BY 1, 2 ORDER BY 3 DESC;
