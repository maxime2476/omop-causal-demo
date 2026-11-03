# ETL Synthea → OMOP CDM 5.4

> Données **synthétiques** (Synthea). Ce document décrit les règles du package OHDSI
> `ETLSyntheaBuilder` 2.1 (dépôt OHDSI/ETL-Synthea, SQL `inst/sql/sql_server/cdm_version/v540/`),
> telles que lues dans son code source, et les choix faits pour ce dépôt.

## Chaîne d'exécution

| Étape | Commande | Rôle |
|---|---|---|
| 1 | `bash scripts/generate_synthea.sh` | Synthea v3.3.0, 1 000 patients, graines 42, dates de référence et de fin 2025-12-31, export CSV seul |
| 2 | `docker compose up -d` | PostgreSQL 16, schémas `cdm`, `native`, `results` créés à l'init |
| 3 | `Rscript r/02_synthea_etl_postgres.R` | ETL complet + vérifications → `output/etl/` |

Étapes du script R (ordre de `extras/codeToRun.R`) : `CreateCDMTables` → `CreateSyntheaTables` →
`LoadSyntheaTables` → `LoadVocabFromCsv` → `CreateMapAndRollupTables` → `CreateExtraIndices` →
`LoadEventTables`.

**Version de Synthea** : `CreateSyntheaTables` n'accepte que 2.7.0, 3.0.0, 3.1.0, 3.2.0 et 3.3.0.
Synthea publie des versions plus récentes (3.4.0, 4.0.0), mais leur format CSV n'est pas pris en
charge : on fige donc la 3.3.0.

**Reproductibilité vérifiée** (deux générations successives, lignes triées) : les 16 CSV
cliniques sont identiques (patients, encounters, conditions, medications, observations,
procedures…). Deux fichiers varient : `claims_transactions.csv` (identifiants aléatoires) et
`payers.csv` (agrégats dépendant de l'ordre des threads). Ils n'alimentent que les tables de
coûts et de couverture (`cost`, `payer_plan_period`). Sans l'option `-e` (date de fin), les
rencontres en cours sont closes à l'heure d'exécution : la date de fin est donc fixée au
2025-12-31.

## Correspondance des tables

| Table OMOP | Source Synthea (CSV) | Règle principale |
|---|---|---|
| `person` | `patients` | sexe `M`/`F` → 8507/8532 ; race `white`/`black`/`asian` → 8527/8516/8515 ; `person_source_value` = UUID patient |
| `observation_period` | `encounters` | une période par personne : de la première à la dernière rencontre (`period_type_concept_id` 32882) |
| `visit_occurrence` | `encounters` (après regroupement) | `inpatient` → 9201 ; `emergency`/`urgentcare` → 9203 ; `ambulatory`/`wellness`/`outpatient` → 9202 |
| `visit_detail` | `encounters` | une ligne par rencontre rattachée à une visite |
| `condition_occurrence` | `conditions` | codes SNOMED (ou ICD10CM) → concept standard via `Maps to`, domaine cible Condition |
| `drug_exposure` | `medications`, `immunizations` | RxNorm (médicaments) et CVX (vaccins) → domaine Drug |
| `procedure_occurrence` | `procedures` | SNOMED/HCPCS → domaine Procedure |
| `measurement` | `observations`, `procedures` | LOINC (et SNOMED) → domaine Measurement ; unités UCUM |
| `observation` | `observations`, `conditions`, `allergies` | codes dont le domaine standard est Observation |
| `device_exposure` | `devices` | SNOMED → domaine Device |
| `death` | `encounters` (code SNOMED 308646001) | date = rencontre de certification de décès ; cause = `reasoncode` mappé |
| `condition_era`, `drug_era` | tables CDM | dérivées des occurrences (règles OHDSI standard) |
| `cdm_source` | — | renseignée par `LoadEventTables` |

Le routage par **domaine** suit le vocabulaire standard : un code source va dans la table du
domaine de son concept standard cible (`source_to_standard_vocab_map`, relation `Maps to`,
concepts et relations valides). Ce n'est pas la table d'origine du CSV qui décide.

## Choix sur les visites (`CreateMapAndRollupTables`)

1. **Hospitalisations** : les rencontres `inpatient` d'un même patient séparées d'au plus 1 jour
   sont fusionnées en une seule visite.
2. **Urgences** : les rencontres `emergency`/`urgent` qui commencent le même jour sont fusionnées.
3. **Ambulatoire** : les rencontres `ambulatory`/`wellness`/`outpatient` qui commencent le même
   jour sont fusionnées.
4. **Rattachement** (`ASSIGN_ALL_VISIT_IDS`, `FINAL_VISIT_IDS`) : une rencontre d'urgences ou
   ambulatoire survenue pendant une hospitalisation est rattachée à cette hospitalisation
   (priorité 1). Sinon, elle est rattachée à une visite de même type (priorité 2).

### Limite constatée avec Synthea 3.3.0

Valeurs mesurées après chargement, avec `sql/etl/diagnostics.sql` (sortie :
`output/etl/diagnostics.txt`), sur la génération de référence (graine 42, fin au 2025-12-31) :

| Classe Synthea | Rencontres | Prise en charge par le regroupement | Rattachées à une visite |
|---|---:|---|---:|
| ambulatory | 34 694 | oui | 34 694 |
| wellness | 12 134 | oui | 12 134 |
| outpatient | 7 545 | oui | 7 545 |
| urgentcare | 2 905 | **non** : le regroupement attend `urgent` | 10 |
| emergency | 2 405 | oui | 2 405 |
| inpatient | 955 | oui | 955 |
| virtual | 200 | **non** | 14 |
| snf | 133 | **non** | 114 |
| hospice | 107 | **non** | 2 |
| home | 100 | **non** | 0 |

Les classes non prises en charge (3 445 rencontres, 5,6 %) ne créent pas de visite propre. Seules
140 d'entre elles sont rattachées, parce qu'elles tombent pendant une hospitalisation. Les
événements liés sont **conservés** (jointure externe sur `FINAL_VISIT_IDS`), mais leur
`visit_occurrence_id` est vide :

| Table OMOP | Lignes | Sans `visit_occurrence_id` | % |
|---|---:|---:|---:|
| condition_occurrence | 15 509 | 694 | 4,5 |
| drug_exposure | 64 797 | 9 345 | 14,4 |
| procedure_occurrence | 63 136 | 6 513 | 10,3 |
| measurement | 527 553 | 90 099 | 17,1 |
| observation | 265 019 | 45 821 | 17,3 |

C'est un défaut de l'ETL, pas de Synthea. La correction serait d'ajouter `urgentcare` au groupe
des urgences et de traiter `virtual`/`home`/`snf`/`hospice`. Elle n'est pas appliquée ici pour
garder l'ETL OHDSI tel quel et documenter son comportement.

### Routage des conditions Synthea par domaine

Sur 35 095 lignes de `conditions.csv` (jointure sur le mapping SNOMED), 14 877 vont vers un
concept standard du domaine Condition, **19 274 vers le domaine Observation** et 944 ne sont pas
mappées. Synthea code en « conditions » des *findings* SNOMED sociaux ou administratifs
(« Medication review due », emploi, stress, violences conjugales…). OMOP les classe en
Observation : c'est le comportement attendu du routage par domaine. Cela explique que
`condition_occurrence` (15 509 lignes) soit bien plus petite que le fichier source.

## Vocabulaires requis (Athena)

D'après les filtres `vocabulary_id` du SQL de l'ETL : SNOMED, LOINC, RxNorm, CVX, UCUM, ICD10CM,
HCPCS ; plus RxNorm Extension (cibles de mapping) et les vocabulaires obligatoires d'Athena
(Gender, Race, Visit, Domain…). ICD10 (OMS), et CIM10 s'il est disponible, servent à la phase 5.

## Chargement des vocabulaires

`LoadVocabFromCsv` lit chaque CSV en mémoire, puis l'insère par JDBC (`bulkLoad = FALSE`) ou via
`psql` (`bulkLoad = TRUE`, client PostgreSQL local et `POSTGRES_PATH` requis). Le mode est réglé par
`VOCAB_BULK_LOAD` dans `.env`.
