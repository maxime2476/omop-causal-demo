"""Covariables baseline mesurées avant t0."""

import logging
from dataclasses import dataclass

import duckdb
import pandas as pd

from omop_causal.config import CovariateConfig
from omop_causal.sql import load_sql

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Covariates:
    """Matrice de covariables (index ``person_id``) et libellés lisibles."""

    X: pd.DataFrame
    labels: pd.DataFrame  # colonnes : covariate, label, kind


def _binary_features(
    long: pd.DataFrame, prefix: str, persons: pd.Index, min_prev: float
) -> pd.DataFrame:
    """Pivote (person_id, concept_id) en indicateurs 0/1 filtrés par prévalence."""
    if long.empty:
        return pd.DataFrame(index=persons)
    long = long.assign(value=1, covariate=prefix + long["concept_id"].astype(str))
    wide = (
        long.pivot_table(index="person_id", columns="covariate", values="value", aggfunc="max")
        .reindex(persons)
        .fillna(0)
        .astype(int)
    )
    prev = wide.mean()
    keep = prev[(prev >= min_prev) & (prev <= 1 - min_prev)].index
    logger.info("%s : %d/%d indicateurs retenus (prévalence)", prefix, len(keep), wide.shape[1])
    return wide[sorted(keep)]


def build_covariates(
    con: duckdb.DuckDBPyConnection,
    cohort: pd.DataFrame,
    exclude_drug_ids: tuple[int, ...],
    cfg: CovariateConfig,
) -> Covariates:
    """Construit âge, sexe, année d'index, visites, conditions et médicaments avant t0."""
    index = pd.DataFrame(
        {"person_id": cohort["person_id"], "t0": pd.to_datetime(cohort["t0"]).dt.date}
    )
    con.register("cohort_index", index)
    try:
        params = {"lookback_days": cfg.lookback_days}
        demo = con.execute(load_sql("covariates/demographics.sql")).df().set_index("person_id")
        visits = con.execute(load_sql("covariates/visits_window.sql"), params).df()
        cond = con.execute(load_sql("covariates/conditions_window.sql"), params).df()
        drugs = con.execute(
            load_sql("covariates/drugs_window.sql"),
            {**params, "exclude_ids": list(exclude_drug_ids)},
        ).df()
        cond_any = (
            con.execute(load_sql("covariates/conditions_any_prior.sql")).df()
            if cfg.include_condition_any_time_prior
            else pd.DataFrame(columns=["person_id", "concept_id"])
        )
    finally:
        con.unregister("cohort_index")

    persons = pd.Index(cohort["person_id"], name="person_id")
    lb = cfg.lookback_days
    blocks = [
        demo.reindex(persons)[["age", "male", "index_year"]].astype(float),
        visits.set_index("person_id")["n_visits"]
        .reindex(persons)
        .fillna(0)
        .astype(float)
        .to_frame(f"n_visits_{lb}d"),
        _binary_features(cond, f"cond{lb}d_", persons, cfg.min_prevalence),
        _binary_features(cond_any, "condany_", persons, cfg.min_prevalence),
        _binary_features(drugs, f"drug{lb}d_", persons, cfg.min_prevalence),
    ]
    X = pd.concat(blocks, axis=1)
    if X.isna().any().any():
        raise ValueError("Valeurs manquantes dans les covariables")
    labels = _labels(con, list(X.columns))
    logger.info("%d covariables pour %d personnes", X.shape[1], X.shape[0])
    return Covariates(X=X, labels=labels)


def _labels(con: duckdb.DuckDBPyConnection, columns: list[str]) -> pd.DataFrame:
    """Libellés lisibles : nom du concept OMOP et fenêtre temporelle."""
    ids = sorted({int(c.split("_")[-1]) for c in columns if c.split("_")[-1].isdigit()})
    names = dict(
        con.execute(
            "SELECT concept_id, concept_name FROM concept WHERE list_contains($ids, concept_id)",
            {"ids": ids},
        ).fetchall()
    )
    fixed = {"age": "Âge (années)", "male": "Sexe masculin", "index_year": "Année d'index"}
    rows = []
    for col in columns:
        prefix, _, suffix = col.rpartition("_")
        if col in fixed:
            label, kind = fixed[col], "demographics"
        elif col.startswith("n_visits"):
            label, kind = f"Nombre de visites ({col.removeprefix('n_visits_')})", "visits"
        else:
            window = "tout antécédent" if prefix == "condany" else prefix[-4:]
            kind = "drug" if prefix.startswith("drug") else "condition"
            label = f"{names.get(int(suffix), suffix)} [{window}]"
        rows.append({"covariate": col, "label": label, "kind": kind})
    return pd.DataFrame(rows)
