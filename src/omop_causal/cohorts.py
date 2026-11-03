"""Construction des cohortes « nouveaux utilisateurs », attrition et données de suivi.

Conception (émulation d'essai cible) : éligibilité, assignation et début du suivi sont tous
alignés sur t0 = date de première exposition, ce qui évite le biais de temps immortel.
"""

import logging
from dataclasses import dataclass

import duckdb
import pandas as pd

from omop_causal.config import CohortConfig, FollowUpConfig
from omop_causal.sql import load_sql

logger = logging.getLogger(__name__)

DATE_COLUMNS = (
    "t0",
    "first_target_date",
    "first_comparator_date",
    "first_other_date",
    "obs_start_date",
    "obs_end_date",
    "exposure_end_date",
)


@dataclass(frozen=True)
class CohortResult:
    """Cohorte éligible (une ligne par personne) et flowchart d'attrition."""

    cohort: pd.DataFrame
    attrition: pd.DataFrame


def _to_datetime(df: pd.DataFrame, columns: tuple[str, ...]) -> pd.DataFrame:
    out = df.copy()
    for col in columns:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col])
    return out


def load_candidates(
    con: duckdb.DuckDBPyConnection, target_ids: tuple[int, ...], comparator_ids: tuple[int, ...]
) -> pd.DataFrame:
    """Exécute ``sql/cohorts/new_users.sql`` : une ligne par personne exposée."""
    df = con.execute(
        load_sql("cohorts/new_users.sql"),
        {"target_ids": list(target_ids), "comparator_ids": list(comparator_ids)},
    ).df()
    logger.info("%d personnes exposées à la cible ou au comparateur", len(df))
    return _to_datetime(df, DATE_COLUMNS)


def load_outcome_dates(
    con: duckdb.DuckDBPyConnection, outcome_ids: tuple[int, ...]
) -> pd.DataFrame:
    """Exécute ``sql/cohorts/outcome.sql`` : toutes les dates d'occurrence du critère."""
    df = con.execute(load_sql("cohorts/outcome.sql"), {"outcome_ids": list(outcome_ids)}).df()
    return _to_datetime(df, ("outcome_date",))


def _attrition_row(step: str, df: pd.DataFrame) -> dict[str, object]:
    n_target = int((df["arm"] == "target").sum())
    n_comp = int((df["arm"] == "comparator").sum())
    return {"step": step, "n_target": n_target, "n_comparator": n_comp, "n_total": len(df)}


def has_outcome_before(
    cohort: pd.DataFrame, outcome_dates: pd.DataFrame, lookback_days: int | None = None
) -> pd.Series:
    """Indique, par ligne de ``cohort``, un critère survenu avant ou le jour de t0.

    ``lookback_days=None`` : tout l'historique ; sinon fenêtre ``[t0 - lookback, t0]``.
    """
    merged = cohort[["person_id", "t0"]].merge(outcome_dates, on="person_id", how="inner")
    mask = merged["outcome_date"] <= merged["t0"]
    if lookback_days is not None:
        mask &= merged["outcome_date"] >= merged["t0"] - pd.to_timedelta(lookback_days, unit="D")
    persons = set(merged.loc[mask, "person_id"])
    return cohort["person_id"].isin(persons)


def apply_eligibility(
    candidates: pd.DataFrame,
    outcome_dates: pd.DataFrame,
    cfg: CohortConfig,
    outcome_label: str = "critère",
) -> CohortResult:
    """Applique séquentiellement les critères d'éligibilité et trace l'attrition."""
    df = candidates.copy()
    rows = [_attrition_row("Première exposition à la cible ou au comparateur (t0)", df)]

    df = df[df["obs_start_date"].notna()]
    rows.append(_attrition_row("t0 inclus dans une période d'observation", df))

    prior_days = (df["t0"] - df["obs_start_date"]).dt.days
    df = df[prior_days >= cfg.prior_observation_days]
    rows.append(_attrition_row(f">= {cfg.prior_observation_days} j d'observation avant t0", df))

    if cfg.exclude_prior_other_drug:
        other_before = df["first_other_date"].notna() & (df["first_other_date"] <= df["t0"])
        df = df[~other_before]
        rows.append(_attrition_row("Aucune exposition à l'autre molécule avant ou à t0", df))

    if cfg.exclude_prior_outcome:
        df = df[~has_outcome_before(df, outcome_dates)]
        rows.append(_attrition_row(f"Aucun(e) {outcome_label} avant ou à t0", df))

    df = df.reset_index(drop=True)
    df["treatment"] = (df["arm"] == "target").astype(int)
    attrition = pd.DataFrame(rows)
    attrition["n_excluded"] = (-attrition["n_total"].diff()).fillna(0).astype(int)
    for _, r in attrition.iterrows():
        logger.info("Attrition | %-60s %6d", r["step"], r["n_total"])
    return CohortResult(cohort=df, attrition=attrition)


def _earliest(a: pd.Series, b: pd.Series) -> pd.Series:
    """Minimum élément par élément de deux séries de dates (sans NaT)."""
    return a.where(a <= b, b)


def follow_up(
    cohort: pd.DataFrame,
    outcome_dates: pd.DataFrame,
    cfg: FollowUpConfig,
) -> pd.DataFrame:
    """Calcule temps et événements pour les analyses ITT et « on-treatment ».

    - ITT : de t0 à min(t0 + itt_days, fin d'observation).
    - On-treatment : fin additionnelle à min(fin d'exposition + délai de grâce,
      veille de l'initiation de l'autre molécule).
    Seul le premier critère strictement postérieur à t0 compte.
    """
    df = cohort.copy()
    after = outcome_dates.merge(df[["person_id", "t0"]], on="person_id")
    after = after[after["outcome_date"] > after["t0"]]
    first_event = after.groupby("person_id")["outcome_date"].min()
    df["first_outcome_date"] = df["person_id"].map(first_event)

    itt_end = _earliest(df["t0"] + pd.to_timedelta(cfg.itt_days, unit="D"), df["obs_end_date"])
    ot_end = _earliest(
        df["exposure_end_date"] + pd.to_timedelta(cfg.on_treatment_grace_days, unit="D"), itt_end
    )
    switch = df["first_other_date"] - pd.to_timedelta(1, unit="D")
    ot_end = ot_end.where(~(switch.notna() & (switch > df["t0"]) & (switch < ot_end)), switch)

    for name, end in (("itt", itt_end), ("ot", ot_end)):
        event = df["first_outcome_date"].notna() & (df["first_outcome_date"] <= end)
        stop = df["first_outcome_date"].where(event, end)
        df[f"event_{name}"] = event.astype(int)
        df[f"time_{name}"] = (stop - df["t0"]).dt.days.astype(float)
    return df
