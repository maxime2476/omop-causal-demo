-- Dates d'occurrence d'un critère (condition), descendants inclus.
-- Paramètre : $outcome_ids. Utilisé pour le critère principal et les contrôles négatifs.
SELECT DISTINCT co.person_id, co.condition_start_date AS outcome_date
FROM condition_occurrence co
WHERE list_contains($outcome_ids, co.condition_concept_id)
ORDER BY co.person_id, outcome_date
