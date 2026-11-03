"""Tests de ``describe`` sur une mini-base OMOP construite en mémoire."""

import json
from pathlib import Path

import duckdb
import pytest

from omop_causal.db import DatabaseNotFoundError, connect_duckdb
from omop_causal.describe import (
    MissingCdmTablesError,
    check_required_tables,
    describe,
    write_describe,
)


def _toy_cdm(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute(
        """
        CREATE TABLE person (person_id INTEGER);
        INSERT INTO person VALUES (1), (2);
        CREATE TABLE observation_period (person_id INTEGER,
            observation_period_start_date DATE, observation_period_end_date DATE);
        INSERT INTO observation_period VALUES
            (1, DATE '2000-01-01', DATE '2000-01-11'),
            (2, DATE '2001-01-01', DATE '2001-01-31'),
            (99, DATE '2001-01-01', DATE '2009-01-31');
        CREATE TABLE visit_occurrence (person_id INTEGER);
        CREATE TABLE concept (concept_id INTEGER, concept_name VARCHAR, vocabulary_id VARCHAR);
        INSERT INTO concept VALUES (10, 'Cond A', 'SNOMED'), (20, 'Drug B', 'RxNorm');
        CREATE TABLE concept_ancestor (ancestor_concept_id INTEGER, descendant_concept_id INTEGER);
        CREATE TABLE condition_occurrence (person_id INTEGER, condition_concept_id INTEGER);
        INSERT INTO condition_occurrence VALUES (1, 10), (1, 10), (2, 10);
        CREATE TABLE drug_exposure (person_id INTEGER, drug_concept_id INTEGER);
        INSERT INTO drug_exposure VALUES (2, 20);
        """
    )
    con.close()


@pytest.fixture
def toy_db(tmp_path: Path) -> Path:
    path = tmp_path / "toy.duckdb"
    _toy_cdm(path)
    return path


def test_missing_database_raises(tmp_path: Path) -> None:
    with pytest.raises(DatabaseNotFoundError):
        connect_duckdb(tmp_path / "absent.duckdb")


def test_missing_tables_detected() -> None:
    with pytest.raises(MissingCdmTablesError, match="concept"):
        check_required_tables(["person"])


def test_connection_is_read_only(toy_db: Path) -> None:
    with connect_duckdb(toy_db) as con, pytest.raises(duckdb.Error):
        con.execute("CREATE TABLE x (a INTEGER)")


def test_describe_on_toy_cdm(toy_db: Path, tmp_path: Path) -> None:
    with connect_duckdb(toy_db) as con:
        result = describe(con, top_n=5)

    counts = dict(zip(result.table_counts.table_name, result.table_counts.n_rows, strict=True))
    assert counts["person"] == 2
    assert counts["condition_occurrence"] == 3

    obs = result.observation_summary.iloc[0]
    assert obs["n_persons_with_observation"] == 2
    assert obs["n_orphan_observation_persons"] == 1
    assert obs["median_obs_days"] == pytest.approx(20.0)  # médiane de 10 et 30 jours

    top = result.top_conditions.iloc[0]
    assert (top["concept_id"], top["n_records"], top["n_persons"]) == (10, 3, 2)
    assert result.top_drugs.iloc[0]["concept_name"] == "Drug B"

    out = tmp_path / "out"
    written = write_describe(result, out)
    assert {p.name for p in written} >= {"table_counts.csv", "summary.json"}
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["table_counts"]["person"] == 2
