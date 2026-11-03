"""Point d'entrée en ligne de commande : ``uv run omop-causal <commande>``."""

import logging
from pathlib import Path
from typing import Annotated

import typer

from omop_causal import __version__
from omop_causal.db import DatabaseNotFoundError, connect_duckdb
from omop_causal.describe import describe, write_describe

app = typer.Typer(
    help="Démonstration OMOP-CDM + inférence causale. Données synthétiques uniquement.",
    no_args_is_help=True,
)


def configure_logging(verbose: bool) -> None:
    """Configure le logging racine (niveau INFO, ou DEBUG si ``verbose``)."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Logs détaillés.")] = False,
) -> None:
    """Options globales."""
    configure_logging(verbose)


@app.command()
def version() -> None:
    """Affiche la version du paquet."""
    typer.echo(__version__)


@app.command("describe")
def describe_cmd(
    db: Annotated[
        Path | None, typer.Option(help="Fichier DuckDB (défaut : data/gibleed.duckdb).")
    ] = None,
    out: Annotated[Path, typer.Option(help="Dossier de sortie.")] = Path("output/describe"),
    top_n: Annotated[int, typer.Option(min=1, help="Nombre de concepts dans les tops.")] = 20,
) -> None:
    """Décrit la base OMOP : effectifs, période d'observation, top conditions/médicaments."""
    try:
        con = connect_duckdb(db)
    except DatabaseNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    with con:
        result = describe(con, top_n=top_n)
    write_describe(result, out)
    typer.echo(f"Description écrite dans {out}")


@app.command("study")
def study_cmd(
    config: Annotated[Path, typer.Option(help="Fichier YAML de l'étude.")] = Path(
        "config/study.yaml"
    ),
) -> None:
    """Exécute l'étude observationnelle complète et écrit output/report/."""
    from omop_causal.config import ConfigError, load_config
    from omop_causal.report import write_report
    from omop_causal.study import run_study

    try:
        cfg = load_config(config)
        con = connect_duckdb(cfg.duckdb_path)
    except (ConfigError, DatabaseNotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    with con:
        results = run_study(con, cfg)
    write_report(results, cfg)
    for note in results.notes:
        typer.echo(f"NOTE : {note}")
    typer.echo(f"Rapport écrit dans {cfg.report_dir}")


@app.command("dqd-summary")
def dqd_summary_cmd(
    json_path: Annotated[Path, typer.Option("--json", help="JSON du DQD.")] = Path(
        "output/dqd/dqd_results.json"
    ),
    out: Annotated[Path, typer.Option(help="Dossier de sortie.")] = Path("output/dqd"),
) -> None:
    """Synthétise les résultats du Data Quality Dashboard par catégorie."""
    from omop_causal.dqd import DqdResultsError, write_dqd_summary

    try:
        write_dqd_summary(json_path, out)
    except DqdResultsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Synthèse DQD écrite dans {out}")


if __name__ == "__main__":
    app()
