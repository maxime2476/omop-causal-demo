"""Chargement et validation de ``config/study.yaml``."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PRIMARY_ANALYSES = ("unadjusted", "matched", "iptw_ate", "iptw_att")


class ConfigError(ValueError):
    """Configuration d'étude invalide."""


@dataclass(frozen=True)
class ConceptSpec:
    """Spécification d'un concept à résoudre dans ``CONCEPT``."""

    label: str
    concept_name: str
    vocabulary_id: str
    concept_class_id: str
    expected_concept_id: int | None = None


@dataclass(frozen=True)
class NegativeControlSpec:
    """Condition utilisée comme contrôle négatif (concept standard)."""

    concept_id: int
    label: str


@dataclass(frozen=True)
class CohortConfig:
    """Critères d'éligibilité."""

    prior_observation_days: int
    exclude_prior_outcome: bool
    exclude_prior_other_drug: bool


@dataclass(frozen=True)
class FollowUpConfig:
    """Fenêtres de suivi."""

    itt_days: int
    on_treatment_grace_days: int


@dataclass(frozen=True)
class CovariateConfig:
    """Paramètres des covariables baseline."""

    lookback_days: int
    include_condition_any_time_prior: bool
    min_prevalence: float


@dataclass(frozen=True)
class PropensityConfig:
    """Paramètres du score de propension."""

    cv_folds: int
    c_grid: tuple[float, ...]
    caliper_sd: float
    iptw_truncation_percentiles: tuple[float, float]


@dataclass(frozen=True)
class SensitivityConfig:
    """Analyses de sensibilité."""

    rare_outcome_threshold: float
    calipers: tuple[float, ...]
    truncations: tuple[tuple[float, float], ...]
    negative_control_prior_lookback_days: int
    negative_controls: tuple[NegativeControlSpec, ...]


@dataclass(frozen=True)
class StudyConfig:
    """Configuration complète de l'étude."""

    name: str
    duckdb_path: Path
    output_dir: Path
    seed: int
    target: ConceptSpec
    comparator: ConceptSpec
    outcome: ConceptSpec
    cohort: CohortConfig
    follow_up: FollowUpConfig
    covariates: CovariateConfig
    propensity: PropensityConfig
    smd_threshold: float
    min_events_per_arm: int
    primary_analysis: str
    sensitivity: SensitivityConfig

    @property
    def report_dir(self) -> Path:
        """Dossier des tables et figures."""
        return self.output_dir / "report"


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"Section manquante ou invalide : {key}")
    return value


def _pair(values: Any, name: str) -> tuple[float, float]:
    if not (isinstance(values, list | tuple) and len(values) == 2):
        raise ConfigError(f"{name} doit être une paire [bas, haut]")
    low, high = float(values[0]), float(values[1])
    if not 0 <= low < high <= 100:
        raise ConfigError(f"{name} : percentiles invalides {values}")
    return low, high


def parse_config(raw: dict[str, Any]) -> StudyConfig:
    """Construit une :class:`StudyConfig` depuis un dictionnaire (YAML parsé)."""
    try:
        concepts = _section(raw, "concepts")
        cohort = _section(raw, "cohort")
        follow = _section(raw, "follow_up")
        cov = _section(raw, "covariates")
        ps = _section(raw, "propensity")
        sens = _section(raw, "sensitivity")
        cfg = StudyConfig(
            name=str(raw["name"]),
            duckdb_path=Path(_section(raw, "database")["duckdb_path"]),
            output_dir=Path(raw.get("output_dir", "output")),
            seed=int(raw.get("seed", 0)),
            target=ConceptSpec(**concepts["target"]),
            comparator=ConceptSpec(**concepts["comparator"]),
            outcome=ConceptSpec(**concepts["outcome"]),
            cohort=CohortConfig(**cohort),
            follow_up=FollowUpConfig(**follow),
            covariates=CovariateConfig(**cov),
            propensity=PropensityConfig(
                cv_folds=int(ps["cv_folds"]),
                c_grid=tuple(float(c) for c in ps["c_grid"]),
                caliper_sd=float(ps["caliper_sd"]),
                iptw_truncation_percentiles=_pair(
                    ps["iptw_truncation_percentiles"], "iptw_truncation_percentiles"
                ),
            ),
            smd_threshold=float(_section(raw, "balance")["smd_threshold"]),
            min_events_per_arm=int(_section(raw, "outcome_model")["min_events_per_arm"]),
            primary_analysis=str(_section(raw, "outcome_model")["primary_analysis"]),
            sensitivity=SensitivityConfig(
                rare_outcome_threshold=float(sens["rare_outcome_threshold"]),
                calipers=tuple(float(c) for c in sens["calipers"]),
                truncations=tuple(_pair(t, "truncations") for t in sens["truncations"]),
                negative_control_prior_lookback_days=int(
                    sens["negative_control_prior_lookback_days"]
                ),
                negative_controls=tuple(
                    NegativeControlSpec(concept_id=int(nc["concept_id"]), label=str(nc["label"]))
                    for nc in (sens.get("negative_controls") or [])
                ),
            ),
        )
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"Configuration invalide : {exc}") from exc
    if cfg.primary_analysis not in PRIMARY_ANALYSES:
        raise ConfigError(f"primary_analysis doit être dans {PRIMARY_ANALYSES}")
    if cfg.propensity.caliper_sd <= 0 or cfg.follow_up.itt_days <= 0:
        raise ConfigError("caliper_sd et itt_days doivent être > 0")
    return cfg


def load_config(path: Path) -> StudyConfig:
    """Lit et valide un fichier YAML de configuration."""
    if not path.is_file():
        raise ConfigError(f"Fichier de configuration introuvable : {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"YAML invalide : {path}")
    return parse_config(raw)
