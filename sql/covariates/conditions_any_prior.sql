-- Conditions survenues à tout moment avant t0 (antécédents). Vue requise : cohort_index.
SELECT DISTINCT ci.person_id, co.condition_concept_id AS concept_id
FROM cohort_index ci
JOIN condition_occurrence co
  ON co.person_id = ci.person_id
 AND co.condition_start_date < ci.t0
WHERE co.condition_concept_id <> 0
