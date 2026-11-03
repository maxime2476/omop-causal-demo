"""Description d'une base OMOP-CDM : effectifs, période d'observation, top concepts."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

from omop_causal.db import list_tables

logger = logging.getLogger(__name__)

REQUIRED_TABLES: tuple[str, ...] = (
    "person",
    "observation_period",
    "visit_occurrence",
    "condition_occurrence",
    "drug_exposure",
    "concept",
    "concept_ancestor",
)


class MissingCdmTablesError(RuntimeError):
    """Des tables CDM obligatoires sont absentes."""


@dataclass(frozen=True)
class DescribeResult:
    """Résultats de la description, écrits ensuite sur disque."""

    table_counts: pd.DataFrame
    observation_summary: pd.DataFrame
    top_conditions: pd.DataFrame
    top_drugs: pd.DataFrame


def check_required_tables(tables: list[str]) -> None:
    """Vérifie la présence des tables CDM attendues.

    Raises:
        MissingCdmTablesError: si au moins une table manque.
    """
    missing = sorted(set(REQUIRED_TABLES) - set(tables))
    if missing:
        raise MissingCdmTablesError(f"Tables CDM manquantes : {', '.join(missing)}")


def table_counts(con: duckdb.DuckDBPyConnection, tables: list[str]) -> pd.DataFrame:
    """Nombre de lignes de chaque table."""
    rows = [
        (t, int(con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]))  # type: ignore[index]
        for t in tables
    ]
    return pd.DataFrame(rows, columns=["table_name", "n_rows"])


def observation_summary(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Période d'observation globale et durée d'observation par personne.

    Les statistiques portent sur les personnes présentes dans ``person`` ; les
    périodes d'observation orphelines (``person_id`` absent de ``person``) sont
    comptées à part (intégrité référentielle).
    """
    return con.execute(
        """
        WITH per_person AS (
            SELECT op.person_id,
                   min(op.observation_period_start_date) AS start_date,
                   max(op.observation_period_end_date) AS end_date,
                   sum(date_diff('day', op.observation_period_start_date,
                                 op.observation_period_end_date)) AS obs_days
            FROM observation_period op
            JOIN person p ON p.person_id = op.person_id
            GROUP BY op.person_id
        )
        SELECT (SELECT count(*) FROM person) AS n_persons,
               (SELECT count(DISTINCT op.person_id) FROM observation_period op
                 WHERE op.person_id NOT IN (SELECT person_id FROM person))
                   AS n_orphan_observation_persons,
               count(*) AS n_persons_with_observation,
               min(start_date) AS first_start_date,
               max(end_date) AS last_end_date,
               median(obs_days) AS median_obs_days,
               quantile_cont(obs_days, 0.25) AS q1_obs_days,
               quantile_cont(obs_days, 0.75) AS q3_obs_days
        FROM per_person
        """
    ).df()


def top_concepts(
    con: duckdb.DuckDBPyConnection, table: str, concept_col: str, n: int = 20
) -> pd.DataFrame:
    """Top ``n`` concepts d'une table d'événements (lignes et personnes distinctes)."""
    if table not in {"condition_occurrence", "drug_exposure"}:
        raise ValueError(f"Table non supportée : {table}")
    return con.execute(
        f"""
        SELECT e.{concept_col} AS concept_id,
               c.concept_name,
               c.vocabulary_id,
               count(*) AS n_records,
               count(DISTINCT e.person_id) AS n_persons
        FROM {table} e
        LEFT JOIN concept c ON c.concept_id = e.{concept_col}
        GROUP BY ALL
        ORDER BY n_persons DESC, n_records DESC, concept_id
        LIMIT ?
        """,
        [n],
    ).df()


def describe(con: duckdb.DuckDBPyConnection, top_n: int = 20) -> DescribeResult:
    """Calcule la description complète de la base."""
    tables = list_tables(con)
    logger.info("%d tables trouvées", len(tables))
    check_required_tables(tables)
    return DescribeResult(
        table_counts=table_counts(con, tables),
        observation_summary=observation_summary(con),
        top_conditions=top_concepts(con, "condition_occurrence", "condition_concept_id", top_n),
        top_drugs=top_concepts(con, "drug_exposure", "drug_concept_id", top_n),
    )


def write_describe(result: DescribeResult, out_dir: Path) -> list[Path]:
    """Écrit les CSV et un résumé JSON dans ``out_dir``; retourne les fichiers créés."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "table_counts.csv": result.table_counts,
        "observation_summary.csv": result.observation_summary,
        "top20_conditions.csv": result.top_conditions,
        "top20_drugs.csv": result.top_drugs,
    }
    written: list[Path] = []
    for name, df in files.items():
        path = out_dir / name
        df.to_csv(path, index=False)
        written.append(path)
    counts = dict(
        zip(result.table_counts["table_name"], result.table_counts["n_rows"], strict=True)
    )
    summary = {
        "table_counts": {k: int(v) for k, v in counts.items()},
        "observation": json.loads(
            result.observation_summary.to_json(orient="records", date_format="iso")
        )[0],
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    written.append(summary_path)
    for p in written:
        logger.info("Écrit : %s", p)
    return written
