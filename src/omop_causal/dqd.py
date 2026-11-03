"""Synthèse des résultats JSON de l'OHDSI Data Quality Dashboard (phase C)."""

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

KEEP_COLUMNS = (
    "checkId",
    "checkName",
    "checkLevel",
    "category",
    "subcategory",
    "context",
    "cdmTableName",
    "cdmFieldName",
    "conceptId",
    "unitConceptId",
    "numViolatedRows",
    "pctViolatedRows",
    "numDenominatorRows",
    "thresholdValue",
    "failed",
    "passed",
    "isError",
    "notApplicable",
    "notApplicableReason",
    "error",
    "checkDescription",
)


class DqdResultsError(ValueError):
    """Fichier de résultats DQD invalide."""


def load_dqd_results(path: Path) -> pd.DataFrame:
    """Lit le JSON produit par ``executeDqChecks`` et renvoie une ligne par check.

    Raises:
        DqdResultsError: fichier absent ou sans ``CheckResults``.
    """
    if not path.is_file():
        raise DqdResultsError(f"Résultats DQD introuvables : {path}")
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    checks = raw.get("CheckResults")
    if not isinstance(checks, list) or not checks:
        raise DqdResultsError(f"Aucun 'CheckResults' dans {path}")
    df = pd.DataFrame(checks)
    for col in KEEP_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    df = df[list(KEEP_COLUMNS)].copy()
    for flag in ("failed", "passed", "isError", "notApplicable"):
        df[flag] = pd.to_numeric(df[flag], errors="coerce").fillna(0).astype(int)
    for num in ("numViolatedRows", "pctViolatedRows", "numDenominatorRows", "thresholdValue"):
        df[num] = pd.to_numeric(df[num], errors="coerce")
    df["status"] = "passed"
    df.loc[df["failed"] == 1, "status"] = "failed"
    df.loc[df["notApplicable"] == 1, "status"] = "not_applicable"
    df.loc[df["isError"] == 1, "status"] = "error"
    logger.info("%d checks DQD lus depuis %s", len(df), path)
    return df


def summarize_by_category(df: pd.DataFrame) -> pd.DataFrame:
    """Nombre de checks par catégorie de Kahn et contexte, selon leur statut."""
    out = (
        df.pivot_table(
            index=["category", "context"],
            columns="status",
            values="checkName",
            aggfunc="count",
            fill_value=0,
        )
        .reindex(columns=["passed", "failed", "not_applicable", "error"], fill_value=0)
        .reset_index()
    )
    out.columns.name = None
    out["n_checks"] = out[["passed", "failed", "not_applicable", "error"]].sum(axis=1)
    applicable = out["passed"] + out["failed"]
    out["pct_passed_applicable"] = (100 * out["passed"] / applicable.where(applicable > 0)).round(1)
    return out


def failed_checks(df: pd.DataFrame) -> pd.DataFrame:
    """Checks en échec, triés par catégorie puis part de lignes en violation décroissante."""
    cols = [
        "category",
        "subcategory",
        "context",
        "checkLevel",
        "checkName",
        "cdmTableName",
        "cdmFieldName",
        "conceptId",
        "numViolatedRows",
        "pctViolatedRows",
        "numDenominatorRows",
        "thresholdValue",
        "checkDescription",
    ]
    return (
        df.loc[df["status"] == "failed", cols]
        .sort_values(["category", "pctViolatedRows"], ascending=[True, False])
        .reset_index(drop=True)
    )


def write_dqd_summary(json_path: Path, out_dir: Path) -> list[Path]:
    """Écrit la synthèse par catégorie, la liste des échecs et les erreurs d'exécution."""
    df = load_dqd_results(json_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "dqd_summary_by_category.csv": summarize_by_category(df),
        "dqd_failed_checks.csv": failed_checks(df),
        "dqd_errors.csv": df.loc[
            df["status"] == "error", ["checkName", "cdmTableName", "cdmFieldName", "error"]
        ],
    }
    written = []
    for name, table in files.items():
        path = out_dir / name
        table.to_csv(path, index=False)
        written.append(path)
        logger.info("Écrit : %s (%d lignes)", path, len(table))
    return written
