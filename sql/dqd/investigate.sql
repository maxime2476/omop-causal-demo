-- Investigation des checks DQD en échec (phase C). Exécution :
--   docker exec -i omop-postgres psql -U omop -d omop --csv -q < sql/dqd/investigate.sql
\echo '## [7] observation_type_concept_id utilisés'
SELECT o.observation_type_concept_id, c.concept_name, c.standard_concept, c.invalid_reason, count(*) n
FROM cdm.observation o LEFT JOIN cdm.concept c ON c.concept_id = o.observation_type_concept_id
GROUP BY 1,2,3,4 ORDER BY 5 DESC;
\echo '## [7] concept remplaçant (Maps to / Concept replaced by)'
SELECT cr.concept_id_1, cr.relationship_id, cr.concept_id_2, c2.concept_name, c2.standard_concept
FROM cdm.concept_relationship cr JOIN cdm.concept c2 ON c2.concept_id = cr.concept_id_2
WHERE cr.concept_id_1 IN (SELECT DISTINCT observation_type_concept_id FROM cdm.observation)
  AND cr.relationship_id IN ('Maps to', 'Concept replaced by') AND cr.invalid_reason IS NULL;
\echo '## [1] observation : unités source des lignes avec unit_concept_id = 0'
SELECT coalesce(nullif(unit_source_value, ''), '(vide)') unit_source_value, count(*) n
FROM cdm.observation WHERE unit_concept_id = 0 GROUP BY 1 ORDER BY 2 DESC LIMIT 6;
\echo '## [1] observation : value_as_number renseigné ?'
SELECT (value_as_number IS NOT NULL) has_number, count(*) FROM cdm.observation GROUP BY 1;
\echo '## [6] measurement : unités source des lignes avec unit_concept_id = 0'
SELECT coalesce(nullif(unit_source_value, ''), '(vide)') unit_source_value,
       (value_as_number IS NOT NULL) has_number, count(*) n
FROM cdm.measurement WHERE unit_concept_id = 0 GROUP BY 1,2 ORDER BY 3 DESC LIMIT 10;
\echo '## [6] ces unités existent-elles en UCUM ?'
SELECT m.unit_source_value, count(*) n, max(c.concept_id) ucum_concept_id
FROM cdm.measurement m
LEFT JOIN cdm.concept c ON c.concept_code = m.unit_source_value AND c.vocabulary_id = 'UCUM'
WHERE m.unit_concept_id = 0 AND coalesce(m.unit_source_value, '') <> ''
GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
\echo '## [8] drug_strength : classe des ingredient_concept_id non conformes'
SELECT c.vocabulary_id, c.concept_class_id, count(*) n
FROM cdm.drug_strength ds JOIN cdm.concept c ON c.concept_id = ds.ingredient_concept_id
WHERE c.concept_class_id <> 'Ingredient' GROUP BY 1,2 ORDER BY 3 DESC;
\echo '## [9] drug_exposure.quantity'
SELECT quantity, count(*) FROM cdm.drug_exposure GROUP BY 1 ORDER BY 2 DESC LIMIT 5;
\echo '## [9] Synthea medications.dispenses (source disponible)'
SELECT min(dispenses), percentile_cont(0.5) WITHIN GROUP (ORDER BY dispenses) med, max(dispenses) FROM native.medications;
\echo '## [11][13] days_supply par type d''enregistrement'
SELECT drug_type_concept_id, count(*) n,
       sum((days_supply < 1)::int) lt1, sum((days_supply > 365)::int) gt365,
       max(days_supply) max_ds
FROM cdm.drug_exposure GROUP BY 1 ORDER BY 2 DESC;
\echo '## [11] days_supply < 1 : vaccins (CVX) vs médicaments'
SELECT c.vocabulary_id, count(*) n FROM cdm.drug_exposure d JOIN cdm.concept c ON c.concept_id = d.drug_concept_id
WHERE d.days_supply < 1 GROUP BY 1 ORDER BY 2 DESC;
\echo '## [6] measurement unit_concept_id = 0 : qualitatif (sans valeur numérique) vs numérique'
SELECT (value_as_number IS NOT NULL) AS numerique, count(*) n
FROM cdm.measurement WHERE unit_concept_id = 0 GROUP BY 1;
\echo '## [14] observation_period avant 1950'
SELECT min(observation_period_start_date), count(*) FILTER (WHERE observation_period_start_date < '1950-01-01')
FROM cdm.observation_period;
\echo '## [14] naissance des personnes concernées'
SELECT min(p.year_of_birth), max(p.year_of_birth) FROM cdm.person p
JOIN cdm.observation_period op USING (person_id) WHERE op.observation_period_start_date < '1950-01-01';
\echo '## [16] décès avant 1950'
SELECT d.person_id, d.death_date, p.year_of_birth FROM cdm.death d JOIN cdm.person p USING (person_id)
WHERE d.death_date < '1950-01-01';
\echo '## [15] payer_plan_period : fin après aujourd''hui'
SELECT max(payer_plan_period_end_date), count(*) FILTER (WHERE payer_plan_period_end_date > current_date)
FROM cdm.payer_plan_period;
\echo '## [12] payer_plan_period après le décès : écart en jours'
SELECT min(ppp.payer_plan_period_end_date - d.death_date), percentile_cont(0.5) WITHIN GROUP
       (ORDER BY ppp.payer_plan_period_end_date - d.death_date), max(ppp.payer_plan_period_end_date - d.death_date)
FROM cdm.payer_plan_period ppp JOIN cdm.death d USING (person_id)
WHERE ppp.payer_plan_period_end_date > d.death_date;
\echo '## [10] cancer de l''ovaire chez un homme : source Synthea'
SELECT p.person_id, p.gender_concept_id, p.gender_source_value, co.condition_source_value, co.condition_start_date
FROM cdm.condition_occurrence co JOIN cdm.person p USING (person_id)
WHERE co.condition_concept_id = 200051;
\echo '## [0] condition_status_concept_id'
SELECT condition_status_concept_id, count(*) FROM cdm.condition_occurrence GROUP BY 1;
\echo '## [2-5] admitted_from / discharged_to'
SELECT admitted_from_concept_id, discharged_to_concept_id, count(*) FROM cdm.visit_occurrence GROUP BY 1,2;
