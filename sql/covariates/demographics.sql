-- Âge, sexe et année d'index à t0. Vue requise : cohort_index(person_id, t0).
SELECT ci.person_id,
       year(ci.t0) - p.year_of_birth AS age,
       CASE WHEN p.gender_concept_id = 8507 THEN 1 ELSE 0 END AS male,
       year(ci.t0) AS index_year
FROM cohort_index ci
JOIN person p ON p.person_id = ci.person_id
