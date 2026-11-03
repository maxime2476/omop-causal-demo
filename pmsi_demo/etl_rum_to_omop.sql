-- Phase 5 — ETL d'une table de RUM FICTIVE (pmsi_demo/generate_fake_rum.py) vers OMOP CDM 5.4.
--
-- Usage : bash scripts/run_pmsi_demo.sh   (fixe les variables psql ci-dessous)
--   psql -v cdm_schema=cdm -v rum_csv=data/pmsi/rum_fictif.csv -v out_dir=output/pmsi \
--        -f pmsi_demo/etl_rum_to_omop.sql
--
-- Schémas : `pmsi` (source et tables de travail) et `pmsi_omop` (tables CDM produites), tous deux
-- recréés à chaque exécution. Le vocabulaire (CONCEPT, CONCEPT_RELATIONSHIP) est lu dans
-- :cdm_schema, chargé en phase B depuis l'export Athena, qui contient le vocabulaire français
-- CIM10 (pas le vocabulaire ICD10 OMS). Le CDM Synthea de la phase B n'est pas modifié.
--
-- Correspondances :
--   séjour (n_rss)        -> VISIT_OCCURRENCE (Inpatient Visit)
--   passage en unité (RUM) -> VISIT_DETAIL (rattaché à la visite)
--   DP / DR / DAS          -> CONDITION_OCCURRENCE, PROCEDURE_OCCURRENCE ou OBSERVATION selon le
--                             domaine du concept standard cible (« Maps to »)
--   patient (ipp_fictif)   -> PERSON ; mode de sortie 9 -> DEATH ; OBSERVATION_PERIOD = du premier
--                             au dernier jour d'hospitalisation (le PMSI ne voit que l'hôpital).

\set ON_ERROR_STOP on
\set QUIET on

DROP SCHEMA IF EXISTS pmsi CASCADE;
DROP SCHEMA IF EXISTS pmsi_omop CASCADE;
CREATE SCHEMA pmsi;
CREATE SCHEMA pmsi_omop;

-- 1. Source -------------------------------------------------------------------------------------
CREATE TABLE pmsi.rum (
    n_rss text, n_rum integer, ipp_fictif text, sexe text, date_naissance text,
    unite_medicale text, date_entree text, date_sortie text, mode_entree text, provenance text,
    mode_sortie text, destination text, dp text, dr text, das text, avertissement text
);
-- \copy n'interpole pas les variables psql : la commande est construite avec \set.
\set copy_in '\\copy pmsi.rum FROM ' :'rum_csv' ' WITH (FORMAT csv, HEADER true)'
:copy_in

CREATE TABLE pmsi.rum_typed AS
SELECT r.*,
       to_date(r.date_naissance, 'DDMMYYYY') AS birth_date,
       to_date(r.date_entree, 'DDMMYYYY') AS start_date,
       to_date(r.date_sortie, 'DDMMYYYY') AS end_date
FROM pmsi.rum r;

-- 2. Concepts de référence, retrouvés par requête (jamais codés en dur) --------------------------
-- 0 si aucun concept standard et valide ne correspond : c'est le cas de « Home » (8536), qui
-- n'est plus standard dans cet export Athena.
CREATE TABLE pmsi.ref_concepts AS
SELECT v.role, v.vocabulary_id, v.lookup, coalesce(c.concept_id, 0) AS concept_id,
       c.concept_name
FROM (VALUES
    ('gender_male',        'Gender',               'code', 'M'),
    ('gender_female',      'Gender',               'code', 'F'),
    ('visit_inpatient',    'Visit',                'code', 'IP'),
    ('type_claim',         'Type Concept',         'name', 'Inpatient claim'),
    ('status_primary',     'Condition Status',     'name', 'Primary diagnosis'),
    ('status_secondary',   'Condition Status',     'name', 'Secondary diagnosis'),
    ('place_er',           'CMS Place of Service', 'name', 'Emergency Room - Hospital'),
    ('place_hospital',     'CMS Place of Service', 'name', 'Inpatient Hospital'),
    ('place_rehab',        'CMS Place of Service', 'name',
                           'Comprehensive Inpatient Rehabilitation Facility'),
    ('place_nursing',      'CMS Place of Service', 'name', 'Nursing Facility'),
    ('place_home',         'CMS Place of Service', 'name', 'Home')
) AS v(role, vocabulary_id, kind, lookup)
LEFT JOIN :"cdm_schema".concept c
       ON c.vocabulary_id = v.vocabulary_id
      AND ((v.kind = 'code' AND c.concept_code = v.lookup)
           OR (v.kind = 'name' AND c.concept_name = v.lookup))
      AND c.standard_concept = 'S' AND c.invalid_reason IS NULL;

CREATE FUNCTION pmsi.ref(r text) RETURNS integer LANGUAGE sql STABLE AS
$$ SELECT concept_id FROM pmsi.ref_concepts WHERE role = r $$;

-- 3. Diagnostics en format long, codes normalisés --------------------------------------------
-- Format RUM : code sans point (K922). Format Athena CIM10 : avec point (K92.2).
CREATE TABLE pmsi.diag AS
WITH long AS (
    SELECT n_rss, n_rum, 'DP' AS role, 1::bigint AS rang, dp AS code_rum
    FROM pmsi.rum WHERE coalesce(dp, '') <> ''
    UNION ALL
    SELECT n_rss, n_rum, 'DR', 1, dr FROM pmsi.rum WHERE coalesce(dr, '') <> ''
    UNION ALL
    SELECT r.n_rss, r.n_rum, 'DAS', u.rang, u.code
    FROM pmsi.rum r,
         unnest(string_to_array(nullif(r.das, ''), '|')) WITH ORDINALITY AS u(code, rang)
),
clean AS (
    SELECT *, upper(regexp_replace(code_rum, '[^A-Za-z0-9]', '', 'g')) AS c FROM long
)
SELECT n_rss, n_rum, role, rang, code_rum,
       CASE WHEN length(c) > 3 THEN substr(c, 1, 3) || '.' || substr(c, 4) ELSE c END
           AS code_cim10
FROM clean;

-- 4. Mapping CIM10 -> concepts standards ---------------------------------------------------------
-- Pour chaque code : le code lui-même (niveau 0), puis ses ancêtres par troncature (E66.00 ->
-- E66.0 -> E66). On retient le niveau le plus proche qui possède un « Maps to » valide. Le repli
-- sur un parent concerne surtout les extensions françaises à 6 caractères, dont la plupart
-- n'ont pas de « Maps to » dans Athena : il perd de la précision et il est signalé.
CREATE TABLE pmsi.code_candidates AS
SELECT d.code_cim10, s.lvl,
       rtrim(left(d.code_cim10, length(d.code_cim10) - s.lvl), '.') AS candidate_code
FROM (SELECT DISTINCT code_cim10 FROM pmsi.diag) d,
     generate_series(0, 3) AS s(lvl)
WHERE length(rtrim(left(d.code_cim10, length(d.code_cim10) - s.lvl), '.')) >= 3
  AND (s.lvl = 0 OR rtrim(left(d.code_cim10, length(d.code_cim10) - s.lvl), '.')
                    <> rtrim(left(d.code_cim10, length(d.code_cim10) - s.lvl + 1), '.'));

CREATE TABLE pmsi.candidate_concepts AS
SELECT cc.code_cim10, cc.lvl, cc.candidate_code, c.concept_id AS source_concept_id
FROM pmsi.code_candidates cc
JOIN :"cdm_schema".concept c
  ON c.vocabulary_id = 'CIM10' AND c.concept_code = cc.candidate_code;

-- Une seule lecture de CONCEPT_RELATIONSHIP (non indexée), restreinte aux concepts utiles.
CREATE TABLE pmsi.relations AS
SELECT r.concept_id_1, r.relationship_id, r.concept_id_2
FROM :"cdm_schema".concept_relationship r
WHERE r.relationship_id IN ('Maps to', 'Maps to value')
  AND r.invalid_reason IS NULL
  AND r.concept_id_1 IN (SELECT source_concept_id FROM pmsi.candidate_concepts);

CREATE TABLE pmsi.code_map AS
WITH best AS (
    SELECT DISTINCT ON (cc.code_cim10) cc.code_cim10, cc.lvl, cc.candidate_code,
           cc.source_concept_id
    FROM pmsi.candidate_concepts cc
    WHERE EXISTS (SELECT 1 FROM pmsi.relations r
                  WHERE r.concept_id_1 = cc.source_concept_id AND r.relationship_id = 'Maps to')
    ORDER BY cc.code_cim10, cc.lvl
),
own AS (
    SELECT code_cim10, source_concept_id FROM pmsi.candidate_concepts WHERE lvl = 0
),
codes AS (SELECT DISTINCT code_cim10 FROM pmsi.diag)
SELECT k.code_cim10,
       coalesce(o.source_concept_id, 0) AS source_concept_id,
       CASE WHEN b.lvl = 0 THEN 'direct'
            WHEN b.lvl > 0 AND o.source_concept_id IS NOT NULL THEN 'parent'
            WHEN o.source_concept_id IS NOT NULL THEN 'non_mappe'
            ELSE 'code_inconnu' END AS mapping_status,
       CASE WHEN o.source_concept_id IS NOT NULL THEN b.candidate_code END AS mapped_from_code,
       coalesce(t.concept_id, 0) AS target_concept_id,
       t.concept_name AS target_concept_name,
       coalesce(t.domain_id, 'Condition') AS target_domain,
       (SELECT min(v.concept_id_2) FROM pmsi.relations v
         WHERE v.concept_id_1 = b.source_concept_id AND v.relationship_id = 'Maps to value')
           AS value_as_concept_id
FROM codes k
LEFT JOIN own o ON o.code_cim10 = k.code_cim10
LEFT JOIN best b ON b.code_cim10 = k.code_cim10 AND o.source_concept_id IS NOT NULL
LEFT JOIN pmsi.relations r ON r.concept_id_1 = b.source_concept_id
                          AND r.relationship_id = 'Maps to'
LEFT JOIN :"cdm_schema".concept t ON t.concept_id = r.concept_id_2;

-- 5. Tables CDM (structure copiée du CDM 5.4 de la phase B) ------------------------------------
CREATE TABLE pmsi_omop.person (LIKE :"cdm_schema".person INCLUDING DEFAULTS);
CREATE TABLE pmsi_omop.observation_period (LIKE :"cdm_schema".observation_period INCLUDING DEFAULTS);
CREATE TABLE pmsi_omop.visit_occurrence (LIKE :"cdm_schema".visit_occurrence INCLUDING DEFAULTS);
CREATE TABLE pmsi_omop.visit_detail (LIKE :"cdm_schema".visit_detail INCLUDING DEFAULTS);
CREATE TABLE pmsi_omop.condition_occurrence (LIKE :"cdm_schema".condition_occurrence INCLUDING DEFAULTS);
CREATE TABLE pmsi_omop.procedure_occurrence (LIKE :"cdm_schema".procedure_occurrence INCLUDING DEFAULTS);
CREATE TABLE pmsi_omop.observation (LIKE :"cdm_schema".observation INCLUDING DEFAULTS);
CREATE TABLE pmsi_omop.death (LIKE :"cdm_schema".death INCLUDING DEFAULTS);

CREATE TABLE pmsi.person_map AS
SELECT ipp_fictif, row_number() OVER (ORDER BY ipp_fictif) AS person_id,
       min(sexe) AS sexe, min(birth_date) AS birth_date
FROM pmsi.rum_typed GROUP BY ipp_fictif;

-- Race et origine ethnique : non recueillies en France (concept 0).
INSERT INTO pmsi_omop.person (person_id, gender_concept_id, year_of_birth, month_of_birth,
    day_of_birth, birth_datetime, race_concept_id, ethnicity_concept_id, person_source_value,
    gender_source_value, gender_source_concept_id, race_source_concept_id,
    ethnicity_source_concept_id)
SELECT person_id,
       CASE sexe WHEN '1' THEN pmsi.ref('gender_male') WHEN '2' THEN pmsi.ref('gender_female')
            ELSE 0 END,
       extract(year FROM birth_date), extract(month FROM birth_date), extract(day FROM birth_date),
       birth_date, 0, 0, ipp_fictif, sexe, 0, 0, 0
FROM pmsi.person_map;

CREATE TABLE pmsi.visit_map AS
SELECT n_rss, row_number() OVER (ORDER BY n_rss) AS visit_occurrence_id
FROM (SELECT DISTINCT n_rss FROM pmsi.rum) s;

CREATE TABLE pmsi.detail_map AS
SELECT r.n_rss, r.n_rum, row_number() OVER (ORDER BY r.n_rss, r.n_rum) AS visit_detail_id
FROM pmsi.rum r;

-- Provenance (mode d'entrée + provenance du 1er RUM) et destination (dernier RUM).
CREATE FUNCTION pmsi.place_from(mode text, prov text) RETURNS integer LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN mode = '8' AND prov = '5' THEN pmsi.ref('place_er')
                WHEN mode = '8' THEN pmsi.ref('place_home')
                WHEN mode IN ('6', '7') AND prov = '1' THEN pmsi.ref('place_hospital')
                WHEN mode IN ('6', '7') AND prov = '2' THEN pmsi.ref('place_rehab')
                ELSE 0 END $$;
CREATE FUNCTION pmsi.place_to(mode text, dest text) RETURNS integer LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN mode = '8' AND dest = '7' THEN pmsi.ref('place_nursing')
                WHEN mode = '8' THEN pmsi.ref('place_home')
                WHEN mode IN ('6', '7') AND dest = '1' THEN pmsi.ref('place_hospital')
                WHEN mode IN ('6', '7') AND dest = '2' THEN pmsi.ref('place_rehab')
                ELSE 0 END $$;  -- décès (9) : DEATH, pas de lieu de sortie

INSERT INTO pmsi_omop.visit_occurrence (visit_occurrence_id, person_id, visit_concept_id,
    visit_start_date, visit_start_datetime, visit_end_date, visit_end_datetime,
    visit_type_concept_id, visit_source_value, visit_source_concept_id,
    admitted_from_concept_id, admitted_from_source_value, discharged_to_concept_id,
    discharged_to_source_value)
SELECT vm.visit_occurrence_id, pm.person_id, pmsi.ref('visit_inpatient'),
       f.start_date, f.start_date, l.end_date, l.end_date, pmsi.ref('type_claim'), vm.n_rss, 0,
       pmsi.place_from(f.mode_entree, f.provenance), f.mode_entree || ':' || f.provenance,
       pmsi.place_to(l.mode_sortie, l.destination), l.mode_sortie || ':' || l.destination
FROM pmsi.visit_map vm
JOIN pmsi.rum_typed f ON f.n_rss = vm.n_rss
                     AND f.n_rum = (SELECT min(n_rum) FROM pmsi.rum WHERE n_rss = vm.n_rss)
JOIN pmsi.rum_typed l ON l.n_rss = vm.n_rss
                     AND l.n_rum = (SELECT max(n_rum) FROM pmsi.rum WHERE n_rss = vm.n_rss)
JOIN pmsi.person_map pm ON pm.ipp_fictif = f.ipp_fictif;

INSERT INTO pmsi_omop.visit_detail (visit_detail_id, person_id, visit_detail_concept_id,
    visit_detail_start_date, visit_detail_start_datetime, visit_detail_end_date,
    visit_detail_end_datetime, visit_detail_type_concept_id, visit_detail_source_value,
    visit_detail_source_concept_id, admitted_from_concept_id, admitted_from_source_value,
    discharged_to_concept_id, discharged_to_source_value, preceding_visit_detail_id,
    visit_occurrence_id)
SELECT dm.visit_detail_id, pm.person_id, pmsi.ref('visit_inpatient'),
       r.start_date, r.start_date, r.end_date, r.end_date, pmsi.ref('type_claim'),
       r.unite_medicale, 0,
       pmsi.place_from(r.mode_entree, r.provenance), r.mode_entree || ':' || r.provenance,
       pmsi.place_to(r.mode_sortie, r.destination), r.mode_sortie || ':' || r.destination,
       lag(dm.visit_detail_id) OVER (PARTITION BY r.n_rss ORDER BY r.n_rum),
       vm.visit_occurrence_id
FROM pmsi.rum_typed r
JOIN pmsi.detail_map dm ON dm.n_rss = r.n_rss AND dm.n_rum = r.n_rum
JOIN pmsi.visit_map vm ON vm.n_rss = r.n_rss
JOIN pmsi.person_map pm ON pm.ipp_fictif = r.ipp_fictif;

-- 6. Diagnostics -> tables d'événements selon le domaine du concept cible ----------------------
-- Le PMSI ne date pas les diagnostics : on prend les dates du passage dans l'unité.
CREATE TABLE pmsi.events AS
SELECT row_number() OVER (ORDER BY d.n_rss, d.n_rum, d.role, d.rang, m.target_concept_id)
           AS event_id,
       pm.person_id, vm.visit_occurrence_id, dm.visit_detail_id, r.start_date, r.end_date,
       d.role, d.code_rum, d.code_cim10, m.source_concept_id, m.target_concept_id,
       m.value_as_concept_id, m.mapping_status,
       CASE WHEN m.target_domain IN ('Condition', 'Procedure', 'Observation')
            THEN m.target_domain ELSE 'Observation' END AS target_table_domain,
       CASE WHEN d.role = 'DP' THEN pmsi.ref('status_primary')
            ELSE pmsi.ref('status_secondary') END AS status_concept_id
FROM pmsi.diag d
JOIN pmsi.code_map m ON m.code_cim10 = d.code_cim10
JOIN pmsi.rum_typed r ON r.n_rss = d.n_rss AND r.n_rum = d.n_rum
JOIN pmsi.person_map pm ON pm.ipp_fictif = r.ipp_fictif
JOIN pmsi.visit_map vm ON vm.n_rss = d.n_rss
JOIN pmsi.detail_map dm ON dm.n_rss = d.n_rss AND dm.n_rum = d.n_rum;

-- DR (diagnostic relié) : pas d'équivalent exact dans « Condition Status » -> diagnostic secondaire.
INSERT INTO pmsi_omop.condition_occurrence (condition_occurrence_id, person_id,
    condition_concept_id, condition_start_date, condition_start_datetime, condition_end_date,
    condition_end_datetime, condition_type_concept_id, condition_status_concept_id,
    visit_occurrence_id, visit_detail_id, condition_source_value, condition_source_concept_id,
    condition_status_source_value)
SELECT event_id, person_id, target_concept_id, start_date, start_date, end_date, end_date,
       pmsi.ref('type_claim'), status_concept_id, visit_occurrence_id, visit_detail_id,
       code_rum, source_concept_id, role
FROM pmsi.events WHERE target_table_domain = 'Condition';

INSERT INTO pmsi_omop.procedure_occurrence (procedure_occurrence_id, person_id,
    procedure_concept_id, procedure_date, procedure_datetime, procedure_end_date,
    procedure_end_datetime, procedure_type_concept_id, modifier_concept_id, visit_occurrence_id,
    visit_detail_id, procedure_source_value, procedure_source_concept_id)
SELECT event_id, person_id, target_concept_id, start_date, start_date, end_date, end_date,
       pmsi.ref('type_claim'), 0, visit_occurrence_id, visit_detail_id, code_rum,
       source_concept_id
FROM pmsi.events WHERE target_table_domain = 'Procedure';

INSERT INTO pmsi_omop.observation (observation_id, person_id, observation_concept_id,
    observation_date, observation_datetime, observation_type_concept_id, value_as_concept_id,
    visit_occurrence_id, visit_detail_id, observation_source_value,
    observation_source_concept_id)
SELECT event_id, person_id, target_concept_id, start_date, start_date, pmsi.ref('type_claim'),
       value_as_concept_id, visit_occurrence_id, visit_detail_id, code_rum, source_concept_id
FROM pmsi.events WHERE target_table_domain = 'Observation';

INSERT INTO pmsi_omop.death (person_id, death_date, death_datetime, death_type_concept_id,
    cause_concept_id, cause_source_concept_id)
SELECT pm.person_id, max(r.end_date), max(r.end_date), pmsi.ref('type_claim'), 0, 0
FROM pmsi.rum_typed r JOIN pmsi.person_map pm ON pm.ipp_fictif = r.ipp_fictif
WHERE r.mode_sortie = '9'
GROUP BY pm.person_id;

INSERT INTO pmsi_omop.observation_period (observation_period_id, person_id,
    observation_period_start_date, observation_period_end_date, period_type_concept_id)
SELECT person_id, person_id, min(visit_start_date), max(visit_end_date), pmsi.ref('type_claim')
FROM pmsi_omop.visit_occurrence GROUP BY person_id;

-- 7. Rapport de mapping ------------------------------------------------------------------------
-- Taux = codes (ou occurrences) avec concept standard / total. « strict » : mapping direct du
-- code ; « avec repli » : direct + via le code parent.
CREATE TABLE pmsi.mapping_rate AS
WITH occ AS (SELECT DISTINCT d.n_rss, d.n_rum, d.role, d.rang, d.code_cim10, m.mapping_status
             FROM pmsi.diag d
             JOIN (SELECT DISTINCT code_cim10, mapping_status FROM pmsi.code_map) m
               ON m.code_cim10 = d.code_cim10),
units AS (
    SELECT 'codes distincts' AS unite, mapping_status FROM
        (SELECT DISTINCT code_cim10, mapping_status FROM pmsi.code_map) s
    UNION ALL
    SELECT 'occurrences', mapping_status FROM occ
)
SELECT unite, count(*) AS total,
       count(*) FILTER (WHERE mapping_status = 'direct') AS direct,
       count(*) FILTER (WHERE mapping_status = 'parent') AS via_parent,
       count(*) FILTER (WHERE mapping_status = 'non_mappe') AS connu_non_mappe,
       count(*) FILTER (WHERE mapping_status = 'code_inconnu') AS code_inconnu,
       round(100.0 * count(*) FILTER (WHERE mapping_status = 'direct') / count(*), 1)
           AS taux_strict_pct,
       round(100.0 * count(*) FILTER (WHERE mapping_status IN ('direct', 'parent')) / count(*), 1)
           AS taux_avec_repli_pct
FROM units GROUP BY unite ORDER BY unite;

CREATE TABLE pmsi.code_report AS
SELECT m.code_cim10, src.concept_name AS source_concept_name, m.mapping_status,
       m.mapped_from_code, m.target_concept_id, m.target_concept_name, m.target_domain,
       m.value_as_concept_id,
       (SELECT count(*) FROM pmsi.diag d WHERE d.code_cim10 = m.code_cim10) AS n_occurrences
FROM pmsi.code_map m
LEFT JOIN :"cdm_schema".concept src ON src.concept_id = m.source_concept_id
ORDER BY m.mapping_status, n_occurrences DESC, m.code_cim10;

CREATE TABLE pmsi.table_counts AS
SELECT 'source: rum' AS table_name, count(*) AS n_rows FROM pmsi.rum
UNION ALL SELECT 'source: diagnostics (DP+DR+DAS)', count(*) FROM pmsi.diag
UNION ALL SELECT 'person', count(*) FROM pmsi_omop.person
UNION ALL SELECT 'observation_period', count(*) FROM pmsi_omop.observation_period
UNION ALL SELECT 'visit_occurrence', count(*) FROM pmsi_omop.visit_occurrence
UNION ALL SELECT 'visit_detail', count(*) FROM pmsi_omop.visit_detail
UNION ALL SELECT 'condition_occurrence', count(*) FROM pmsi_omop.condition_occurrence
UNION ALL SELECT 'condition_occurrence (concept 0)', count(*)
          FROM pmsi_omop.condition_occurrence WHERE condition_concept_id = 0
UNION ALL SELECT 'procedure_occurrence', count(*) FROM pmsi_omop.procedure_occurrence
UNION ALL SELECT 'observation', count(*) FROM pmsi_omop.observation
UNION ALL SELECT 'death', count(*) FROM pmsi_omop.death;

-- 8. Couverture de tout le vocabulaire CIM10 (indépendante des codes tirés au hasard) --------
-- Part des concepts CIM10 ayant un « Maps to » valide, par classe et longueur de code. Les
-- extensions françaises (6 caractères et plus) sont les moins couvertes.
CREATE TABLE pmsi.vocab_coverage AS
WITH cim AS (
    SELECT concept_id, concept_class_id, length(concept_code) AS code_length
    FROM :"cdm_schema".concept WHERE vocabulary_id = 'CIM10'
),
mapped AS (
    SELECT DISTINCT r.concept_id_1
    FROM :"cdm_schema".concept_relationship r
    WHERE r.relationship_id = 'Maps to' AND r.invalid_reason IS NULL
      AND r.concept_id_1 IN (SELECT concept_id FROM cim)
)
SELECT c.concept_class_id, c.code_length, count(*) AS n_concepts,
       count(m.concept_id_1) AS n_maps_to,
       round(100.0 * count(m.concept_id_1) / count(*), 1) AS pct_maps_to
FROM cim c LEFT JOIN mapped m ON m.concept_id_1 = c.concept_id
GROUP BY ROLLUP (c.concept_class_id, c.code_length)
ORDER BY c.concept_class_id NULLS LAST, c.code_length NULLS LAST;

\set csv ' WITH (FORMAT csv, HEADER true)'
\set copy_out '\\copy (SELECT * FROM pmsi.mapping_rate) TO ' :'out_mapping_rate' :csv
:copy_out
\set copy_out '\\copy (SELECT * FROM pmsi.code_report) TO ' :'out_code_report' :csv
:copy_out
\set copy_out '\\copy (SELECT * FROM pmsi.table_counts) TO ' :'out_table_counts' :csv
:copy_out
\set copy_out '\\copy (SELECT * FROM pmsi.ref_concepts ORDER BY role) TO ' :'out_ref_concepts' :csv
:copy_out
\set copy_out '\\copy (SELECT * FROM pmsi.vocab_coverage) TO ' :'out_vocab_coverage' :csv
:copy_out

\unset QUIET
SELECT * FROM pmsi.mapping_rate;
SELECT * FROM pmsi.table_counts;
SELECT * FROM pmsi.vocab_coverage;
