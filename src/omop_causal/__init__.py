"""omop_causal : ETL OMOP-CDM, qualité des données et inférence causale (données synthétiques)."""

from importlib.metadata import version

__all__ = ["__version__"]

__version__: str = version("omop-causal")
