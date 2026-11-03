-- Conditions dans la fenêtre [t0 - $lookback_days, t0 - 1]. Vue requise : cohort_index.
SELECT DISTINCT ci.person_id, co.condition_concept_id AS concept_id
FROM cohort_index ci
JOIN condition_occurrence co
  ON co.person_id = ci.person_id
 AND co.condition_start_date BETWEEN ci.t0 - CAST($lookback_days AS INTEGER) AND ci.t0 - 1
WHERE co.condition_concept_id <> 0
