# Data Quality Dashboard — synthèse des checks en échec

> Base **synthétique** (Synthea 3.3.0, 1 000 patients) chargée en OMOP CDM 5.4 par ETL-Synthea
> (phase B). Toutes les valeurs ci-dessous viennent de `output/dqd/` :
> `dqd_results.json` (sortie brute du DQD), `dqd_summary_by_category.csv` et
> `dqd_failed_checks.csv` (produits par `uv run omop-causal dqd-summary`), `investigation.txt`
> (sortie de `sql/dqd/investigate.sql`).

## Exécution

```bash
Rscript r/03_run_dqd.R            # DataQualityDashboard 2.9.1, CDM 5.4, 1 thread (6,1 min)
uv run omop-causal dqd-summary    # synthèse par catégorie + liste des échecs
```

Deux ajustements, documentés dans le script :

- **`COHORT` et `COHORT_DEFINITION` exclues.** Le DQD les cherche dans le schéma résultats alors
  que `CreateCDMTables` les crée (vides) dans le schéma CDM. Cela produisait 46 erreurs
  d'exécution sans rapport avec la qualité des données.
- **Exécution sur 1 thread.** Avec `numThreads > 1`, DataQualityDashboard 2.9.1 remet sa
  connexion à `NULL` avant la déconnexion finale, et le script échoue une fois les résultats
  déjà écrits (bug du package).

## Vue d'ensemble

2 338 checks, **18 en échec**, 0 erreur d'exécution. L'`Overview` du DQD annonce 2 320 checks
réussis (99,23 %), mais il compte comme réussis les checks non applicables (par exemple sur des
tables vides). La synthèse ci-dessous les sépare.

| Catégorie | Contexte | Réussis | Échecs | Non applicables | % réussis (applicables) |
|---|---|---:|---:|---:|---:|
| Conformité | Vérification | 744 | 2 | 155 | 99,7 |
| Conformité | Validation | 88 | 0 | 49 | 100,0 |
| Complétude | Vérification | 346 | 7 | 120 | 98,0 |
| Complétude | Validation | 18 | 0 | 0 | 100,0 |
| Plausibilité | Vérification | 326 | 8 | 184 | 97,6 |
| Plausibilité | Validation | 7 | 1 | 283 | 87,5 |

## Interprétation des 18 échecs

Trois origines possibles : **ETL** (défaut ou choix de l'ETL, corrigeable de notre côté),
**Synthea** (artefact du générateur, l'ETL le transcrit fidèlement), **règle non pertinente**
(le check ne s'applique pas à ce type de source).

### Conformité (2 échecs)

| Check | Table.champ | Violations | Origine | Explication (preuve dans `investigation.txt`) |
|---|---|---:|---|---|
| isStandardValidConcept | OBSERVATION.OBSERVATION_TYPE_CONCEPT_ID | 264 117 / 265 019 (99,7 %) | **ETL** | L'ETL écrit 38000280 « Observation recorded from EHR », un ancien type de concept qui n'est plus standard dans ce vocabulaire. Le standard actuel est 32817 « EHR » (ou 32827 « EHR encounter record », déjà utilisé pour les 902 autres lignes). |
| fkClass | DRUG_STRENGTH.INGREDIENT_CONCEPT_ID | 3 587 / 2 966 568 (0,12 %) | **Règle non pertinente** | Table de vocabulaire fournie telle quelle par Athena : ces 3 587 lignes pointent vers des concepts RxNorm de classe « Precise Ingredient ». Il s'agit du contenu du vocabulaire, pas des données. |

### Complétude (7 échecs) — champs à 0 (« pas de concept »)

| Check | Table.champ | Violations | Origine | Explication |
|---|---|---:|---|---|
| standardConceptRecordCompleteness | CONDITION_OCCURRENCE.CONDITION_STATUS_CONCEPT_ID | 15 509 (100 %) | **Synthea** (donnée absente) | Synthea ne fournit pas de statut de diagnostic (principal, secondaire…). |
| idem | VISIT_OCCURRENCE / VISIT_DETAIL.ADMITTED_FROM_CONCEPT_ID et DISCHARGED_TO_CONCEPT_ID (4 checks) | 55 636 chacun (100 %) | **Synthea** (donnée absente) | Pas de provenance ni de destination de sortie dans `encounters.csv`. |
| idem | OBSERVATION.UNIT_CONCEPT_ID | 265 019 (100 %) | **Règle non pertinente** | Aucune observation n'a de valeur numérique (`value_as_number` toujours vide) ni d'unité source : une unité n'a pas de sens ici. |
| idem | MEASUREMENT.UNIT_CONCEPT_ID | 172 873 / 527 553 (32,8 %) | **Mixte** | 118 411 lignes sont qualitatives (pas de valeur numérique, unité `{nominal}` ou vide) : règle non pertinente. **54 462 lignes numériques sont un défaut de l'ETL** : Synthea écrit les unités UCUM sous une forme qui diffère des `concept_code` OMOP (`U/L` au lieu de `[U]/L`, `{score}` au lieu de `[score]`, `[pH]` au lieu de `pH`, `mL/min/{1.73_m2}`…) et l'ETL ne les rapproche que par égalité stricte. Correctif : une table de correspondance des unités (`source_to_concept_map`). |

### Plausibilité (9 échecs)

| Check | Table.champ | Violations | Origine | Explication |
|---|---|---:|---|---|
| plausibleValueLow | DRUG_EXPOSURE.QUANTITY (< 1e-7) | 64 797 (100 %) | **ETL** | L'ETL écrit `quantity = 0` partout, alors que Synthea fournit `medications.dispenses` (de 1 à 1 158, médiane 1), qui pourrait l'alimenter. |
| plausibleValueLow | DRUG_EXPOSURE.DAYS_SUPPLY (< 1) | 29 524 (45,6 %) | **ETL + Synthea** | 13 297 sont des vaccins (CVX, administration ponctuelle, durée 0 attendue) ; 16 227 sont des médicaments RxNorm avec début = fin dans Synthea. L'ETL calcule `days_supply` = fin − début sans plancher à 1 jour. |
| plausibleValueHigh | DRUG_EXPOSURE.DAYS_SUPPLY (> 365) | 6 732 (10,4 %) | **ETL** (choix) | Synthea décrit les traitements chroniques comme une seule prescription de plusieurs années (maximum 14 575 j). L'ETL en fait une seule exposition au lieu de la découper en délivrances. |
| plausibleGender | CONDITION_OCCURRENCE (200051, cancer de l'ovaire) | 1 | **Synthea** | Le CSV source contient le code ICD10 C56.9 pour un patient `gender = M` : artefact du générateur, transcrit fidèlement. |
| plausibleValueLow | OBSERVATION_PERIOD.START_DATE (< 1950) | 25 (2,5 %) | **Règle non pertinente** | Patients nés entre 1915 et 1948 : Synthea simule toute la vie, rencontres comprises. Le seuil de 1950 vise les bases réelles. |
| plausibleValueLow | DEATH.DEATH_DATE et DEATH_DATETIME (< 1950) — 2 checks | 1 (1,0 %) | **Synthea** (règle peu pertinente) | Un patient né en 1915 et décédé le 1916-12-01 : décès infantile simulé, cohérent dans la logique de Synthea. |
| plausibleBeforeDeath | PAYER_PLAN_PERIOD.END_DATE | 29 / 235 (12,3 %) | **Synthea** | Les contrats d'assurance de Synthea courent jusqu'à leur échéance annuelle, de 31 à 342 jours après le décès (médiane 241). |
| plausibleValueHigh | PAYER_PLAN_PERIOD.END_DATE (> aujourd'hui) | 135 / 9 613 (1,4 %) | **Synthea** | La simulation s'arrête au 2025-12-31, mais les contrats en cours gardent leur date d'échéance (jusqu'au 2026-12-30). |

## Bilan par origine

| Origine | Checks | Lesquels |
|---|---:|---|
| Défaut ou choix de l'ETL | 4 | type de concept d'observation non standard, `quantity` à 0, `days_supply` > 365, `days_supply` < 1 (en partie aussi Synthea) |
| Artefact ou donnée absente de Synthea | 10 | statut de diagnostic, provenance et destination des visites (4), cancer de l'ovaire chez un homme, décès en 1916 (2), contrats après le décès et dans le futur (2) |
| Règle non pertinente pour cette source | 3 | `DRUG_STRENGTH` (vocabulaire Athena), unités d'observation, périodes d'observation avant 1950 |
| Mixte (ETL + règle non pertinente) | 1 | unités de mesure : 54 462 lignes numériques non mappées (ETL), 118 411 qualitatives (règle non pertinente) |
| **Total** | **18** | |

Le DQD ne détecte pas l'anomalie la plus structurante trouvée en phase B : les classes de rencontre
Synthea non prises en charge par le regroupement des visites (de 4,5 % à 17,3 % d'événements
sans `visit_occurrence_id`, voir [`etl_mapping.md`](etl_mapping.md)). En effet,
`visit_occurrence_id` est facultatif dans le CDM. Un contrôle qualité utile combine donc le DQD
et des contrôles propres à l'ETL.

## Corrections possibles (non appliquées)

L'ETL OHDSI est conservé tel quel pour documenter son comportement. Une version maintenue
localement corrigerait :

1. `observation_type_concept_id` → 32817 « EHR ».
2. Une table de correspondance des unités Synthea → UCUM OMOP.
3. `drug_exposure.quantity` alimenté par `medications.dispenses`.
4. Un plancher de `days_supply` à 1 pour les médicaments, et le découpage des traitements chroniques.
5. La prise en charge de `urgentcare`, `virtual`, `home`, `snf`, `hospice` dans le regroupement des visites.
