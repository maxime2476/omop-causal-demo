-- Médicaments dans [t0 - $lookback_days, t0 - 1], hors molécules étudiées ($exclude_ids).
SELECT DISTINCT ci.person_id, de.drug_concept_id AS concept_id
FROM cohort_index ci
JOIN drug_exposure de
  ON de.person_id = ci.person_id
 AND de.drug_exposure_start_date BETWEEN ci.t0 - CAST($lookback_days AS INTEGER) AND ci.t0 - 1
WHERE de.drug_concept_id <> 0
  AND NOT list_contains($exclude_ids, de.drug_concept_id)
