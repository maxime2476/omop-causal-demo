-- Nombre de visites dans [t0 - $lookback_days, t0 - 1]. Vue requise : cohort_index.
SELECT ci.person_id, count(vo.visit_occurrence_id) AS n_visits
FROM cohort_index ci
LEFT JOIN visit_occurrence vo
  ON vo.person_id = ci.person_id
 AND vo.visit_start_date BETWEEN ci.t0 - CAST($lookback_days AS INTEGER) AND ci.t0 - 1
GROUP BY ci.person_id
