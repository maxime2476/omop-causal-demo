"""Chargement des fichiers SQL du dossier ``sql/`` à la racine du dépôt."""

import os
from pathlib import Path

_DEFAULT_SQL_DIR = Path(__file__).resolve().parents[2] / "sql"


def sql_dir() -> Path:
    """Dossier SQL : ``OMOP_SQL_DIR`` si défini, sinon ``<dépôt>/sql``."""
    env = os.environ.get("OMOP_SQL_DIR")
    return Path(env) if env else _DEFAULT_SQL_DIR


def load_sql(relative_path: str) -> str:
    """Lit un fichier SQL, ex. ``load_sql("cohorts/outcome.sql")``.

    Raises:
        FileNotFoundError: si le fichier n'existe pas.
    """
    path = sql_dir() / relative_path
    if not path.is_file():
        raise FileNotFoundError(f"Fichier SQL introuvable : {path}")
    return path.read_text(encoding="utf-8")
