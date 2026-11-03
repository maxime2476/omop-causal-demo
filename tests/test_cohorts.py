"""Cohortes : t0, exclusions et suivi sur une mini-base OMOP construite à la main."""

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from omop_causal.cohorts import apply_eligibility, follow_up, load_candidates, load_outcome_dates
from omop_causal.config import CohortConfig, FollowUpConfig

TARGET, COMPARATOR, OUTCOME = 1118084, 1124300, 192671
COHORT_CFG = CohortConfig(
    prior_observation_days=365, exclude_prior_outcome=True, exclude_prior_other_drug=True
)


@pytest.fixture
def con(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    path = tmp_path / "toy.duckdb"
    c = duckdb.connect(str(path))
    c.execute(
        f"""
        CREATE TABLE person (person_id INTEGER, year_of_birth INTEGER, gender_concept_id INTEGER);
        INSERT INTO person VALUES (1, 1950, 8507), (2, 1960, 8532), (3, 1970, 8507),
                                  (4, 1980, 8532), (5, 1990, 8507);
        CREATE TABLE observation_period (person_id INTEGER,
            observation_period_start_date DATE, observation_period_end_date DATE);
        INSERT INTO observation_period VALUES
            (1, DATE '2000-01-01', DATE '2010-12-31'),
            (2, DATE '2000-01-01', DATE '2010-12-31'),
            (3, DATE '2000-01-01', DATE '2010-12-31'),
            (4, DATE '2004-06-01', DATE '2010-12-31'),  -- < 365 j avant t0
            (5, DATE '2000-01-01', DATE '2005-03-01');  -- fin d'observation pendant le suivi
        CREATE TABLE drug_exposure (person_id INTEGER, drug_concept_id INTEGER,
            drug_exposure_start_date DATE, drug_exposure_end_date DATE);
        INSERT INTO drug_exposure VALUES
            (1, {TARGET}, DATE '2005-01-01', DATE '2005-01-31'),
            (1, {TARGET}, DATE '2003-05-10', DATE '2003-05-20'),     -- première exposition
            (2, {COMPARATOR}, DATE '2005-01-01', DATE '2005-01-01'),
            (3, {TARGET}, DATE '2005-01-01', DATE '2005-01-01'),
            (3, {COMPARATOR}, DATE '2005-01-01', DATE '2005-01-01'), -- même jour : exclu
            (4, {TARGET}, DATE '2005-01-01', DATE '2005-01-01'),
            (5, {COMPARATOR}, DATE '2005-01-01', DATE '2005-01-01'),
            (5, {TARGET}, DATE '2005-02-01', DATE '2005-02-01');     -- switch après t0
        CREATE TABLE drug_era (person_id INTEGER, drug_concept_id INTEGER,
            drug_era_start_date DATE, drug_era_end_date DATE);
        INSERT INTO drug_era VALUES (1, {TARGET}, DATE '2003-05-10', DATE '2003-06-30');
        CREATE TABLE condition_occurrence (person_id INTEGER, condition_concept_id INTEGER,
            condition_start_date DATE);
        INSERT INTO condition_occurrence VALUES
            (2, {OUTCOME}, DATE '2002-01-01'),   -- hémorragie antérieure : exclu
            (1, {OUTCOME}, DATE '2003-08-01');   -- événement à J83
        """
    )
    yield c
    c.close()


def _eligible(con: duckdb.DuckDBPyConnection) -> tuple[pd.DataFrame, pd.DataFrame]:
    cand = load_candidates(con, (TARGET,), (COMPARATOR,))
    outcomes = load_outcome_dates(con, (OUTCOME,))
    res = apply_eligibility(cand, outcomes, COHORT_CFG)
    return res.cohort, res.attrition


def test_t0_is_first_exposure_date(con: duckdb.DuckDBPyConnection) -> None:
    cand = load_candidates(con, (TARGET,), (COMPARATOR,)).set_index("person_id")
    assert cand.loc[1, "t0"] == pd.Timestamp("2003-05-10")
    assert cand.loc[1, "arm"] == "target"
    assert cand.loc[5, "arm"] == "comparator"
    assert cand.loc[5, "first_other_date"] == pd.Timestamp("2005-02-01")
    assert cand.loc[1, "exposure_end_date"] == pd.Timestamp("2003-06-30")  # fin de drug_era


def test_prior_outcome_is_excluded(con: duckdb.DuckDBPyConnection) -> None:
    cohort, _ = _eligible(con)
    assert 2 not in set(cohort["person_id"])


def test_other_criteria_and_attrition(con: duckdb.DuckDBPyConnection) -> None:
    cohort, attrition = _eligible(con)
    assert set(cohort["person_id"]) == {1, 5}  # 3 : même jour ; 4 : observation insuffisante
    assert attrition["n_total"].tolist() == [5, 5, 4, 3, 2]
    assert attrition["n_excluded"].sum() == 3
    assert cohort.set_index("person_id").loc[1, "treatment"] == 1


def test_follow_up_itt_and_on_treatment(con: duckdb.DuckDBPyConnection) -> None:
    cohort, _ = _eligible(con)
    outcomes = load_outcome_dates(con, (OUTCOME,))
    fu = follow_up(cohort, outcomes, FollowUpConfig(itt_days=365, on_treatment_grace_days=30))
    fu = fu.set_index("person_id")
    # Personne 1 : événement le 2003-08-01 = J83 ; fin d'exposition 2003-06-30 + 30 j = J81.
    assert (fu.loc[1, "event_itt"], fu.loc[1, "time_itt"]) == (1, 83.0)
    assert (fu.loc[1, "event_ot"], fu.loc[1, "time_ot"]) == (0, 81.0)
    # Personne 5 : censure ITT à la fin d'observation (J59) ; OT censuré la veille du switch (J30).
    assert (fu.loc[5, "event_itt"], fu.loc[5, "time_itt"]) == (0, 59.0)
    assert fu.loc[5, "time_ot"] == 30.0
