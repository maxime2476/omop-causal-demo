-- Candidats « nouveaux utilisateurs » : une ligne par personne exposée à la cible ou au comparateur.
-- Paramètres : $target_ids, $comparator_ids (listes de concept_id, descendants inclus).
-- t0 = date de la toute première exposition à l'une des deux molécules.
-- Les critères d'éligibilité sont appliqués ensuite en Python (attrition étape par étape).
WITH exposure AS (
    SELECT de.person_id,
           min(CASE WHEN list_contains($target_ids, de.drug_concept_id)
                    THEN de.drug_exposure_start_date END) AS first_target_date,
           min(CASE WHEN list_contains($comparator_ids, de.drug_concept_id)
                    THEN de.drug_exposure_start_date END) AS first_comparator_date
    FROM drug_exposure de
    JOIN person p ON p.person_id = de.person_id
    WHERE list_contains(list_concat($target_ids, $comparator_ids), de.drug_concept_id)
    GROUP BY de.person_id
),
indexed AS (
    SELECT person_id, first_target_date, first_comparator_date,
           least(coalesce(first_target_date, first_comparator_date),
                 coalesce(first_comparator_date, first_target_date)) AS t0
    FROM exposure
),
armed AS (
    SELECT *,
           CASE WHEN first_target_date = t0 THEN 'target' ELSE 'comparator' END AS arm,
           CASE WHEN first_target_date = t0 THEN first_comparator_date
                ELSE first_target_date END AS first_other_date
    FROM indexed
),
era_end AS (
    -- Fin de l'ère d'exposition (drug_era) contenant t0, pour la molécule du bras.
    SELECT a.person_id, max(e.drug_era_end_date) AS era_end_date
    FROM armed a
    JOIN drug_era e
      ON e.person_id = a.person_id
     AND list_contains(CASE WHEN a.arm = 'target' THEN $target_ids ELSE $comparator_ids END,
                       e.drug_concept_id)
     AND a.t0 BETWEEN e.drug_era_start_date AND e.drug_era_end_date
    GROUP BY a.person_id
),
exposure_end AS (
    -- Repli : fin de la délivrance index si aucune ère ne couvre t0.
    SELECT a.person_id, max(de.drug_exposure_end_date) AS index_exposure_end_date
    FROM armed a
    JOIN drug_exposure de
      ON de.person_id = a.person_id
     AND de.drug_exposure_start_date = a.t0
     AND list_contains(CASE WHEN a.arm = 'target' THEN $target_ids ELSE $comparator_ids END,
                       de.drug_concept_id)
    GROUP BY a.person_id
)
SELECT a.person_id,
       a.arm,
       a.t0,
       a.first_target_date,
       a.first_comparator_date,
       a.first_other_date,
       op.observation_period_start_date AS obs_start_date,
       op.observation_period_end_date AS obs_end_date,
       greatest(a.t0, coalesce(ee.era_end_date, x.index_exposure_end_date, a.t0))
           AS exposure_end_date
FROM armed a
LEFT JOIN observation_period op
       ON op.person_id = a.person_id
      AND a.t0 BETWEEN op.observation_period_start_date AND op.observation_period_end_date
LEFT JOIN era_end ee ON ee.person_id = a.person_id
LEFT JOIN exposure_end x ON x.person_id = a.person_id
ORDER BY a.person_id
