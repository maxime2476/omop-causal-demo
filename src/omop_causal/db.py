"""Connexion aux bases OMOP-CDM (DuckDB en lecture seule)."""

import logging
import os
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)

DEFAULT_DUCKDB_PATH = Path("data/gibleed.duckdb")


class DatabaseNotFoundError(FileNotFoundError):
    """Le fichier DuckDB attendu est introuvable."""


def resolve_duckdb_path(path: Path | str | None = None) -> Path:
    """Retourne le chemin DuckDB : argument, puis ``OMOP_DUCKDB_PATH``, puis valeur par défaut."""
    if path is not None:
        return Path(path)
    env_path = os.environ.get("OMOP_DUCKDB_PATH")
    return Path(env_path) if env_path else DEFAULT_DUCKDB_PATH


def connect_duckdb(path: Path | str | None = None) -> duckdb.DuckDBPyConnection:
    """Ouvre une connexion DuckDB **en lecture seule** sur une base OMOP.

    Raises:
        DatabaseNotFoundError: si le fichier n'existe pas.
    """
    db_path = resolve_duckdb_path(path)
    if not db_path.is_file():
        raise DatabaseNotFoundError(
            f"Base DuckDB introuvable : {db_path}. "
            "Lancer d'abord `Rscript r/01_eunomia_to_duckdb.R`."
        )
    logger.info("Connexion DuckDB (lecture seule) : %s", db_path)
    return duckdb.connect(str(db_path), read_only=True)


def list_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Liste les tables de la base (noms en minuscules, triés)."""
    rows = con.execute(
        "SELECT lower(table_name) FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE' ORDER BY 1"
    ).fetchall()
    return [str(r[0]) for r in rows]
