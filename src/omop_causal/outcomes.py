"""Modèles de survie : Cox (HR, IC robustes), Kaplan-Meier, test des risques proportionnels."""

import logging
import warnings
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from lifelines import CoxPHFitter, KaplanMeierFitter
from lifelines.statistics import proportional_hazard_test

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CoxResult:
    """Résultat d'un modèle de Cox à une variable (traitement)."""

    analysis: str
    status: str  # "ok" ou "insufficient_events"
    n_target: int
    n_comparator: int
    events_target: int
    events_comparator: int
    hr: float
    ci_low: float
    ci_high: float
    p_value: float
    ph_test_p: float  # test global des résidus de Schoenfeld (transformation « rank »)

    def as_dict(self) -> dict[str, object]:
        """Représentation dictionnaire (export CSV)."""
        return asdict(self)


def fit_cox(
    df: pd.DataFrame,
    analysis: str,
    time_col: str,
    event_col: str,
    min_events_per_arm: int,
    weights_col: str | None = None,
    cluster_col: str | None = None,
) -> CoxResult:
    """Ajuste un Cox ``Surv(time, event) ~ treatment`` avec variance robuste (sandwich).

    - pondéré (``weights_col``) : IPTW, variance robuste ;
    - apparié : ``cluster_col`` = identifiant de paire (variance robuste groupée).
    Si un bras compte moins de ``min_events_per_arm`` événements, aucun modèle n'est ajusté
    et le statut ``insufficient_events`` est renvoyé (pas de résultat forcé).
    """
    data = df if weights_col is None else df[df[weights_col] > 0]
    t, c = data["treatment"] == 1, data["treatment"] == 0
    ev = data[event_col] == 1
    counts = {
        "n_target": int(t.sum()),
        "n_comparator": int(c.sum()),
        "events_target": int((t & ev).sum()),
        "events_comparator": int((c & ev).sum()),
    }
    if min(counts["events_target"], counts["events_comparator"]) < min_events_per_arm:
        logger.warning(
            "%s : événements insuffisants (%d / %d) — pas de Cox",
            analysis,
            counts["events_target"],
            counts["events_comparator"],
        )
        nan = float("nan")
        return CoxResult(
            analysis=analysis,
            status="insufficient_events",
            **counts,
            hr=nan,
            ci_low=nan,
            ci_high=nan,
            p_value=nan,
            ph_test_p=nan,
        )

    cols = ["treatment", time_col, event_col]
    cols += [x for x in (weights_col, cluster_col) if x is not None]
    model_df = data[cols].copy()
    cph = CoxPHFitter()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cph.fit(
            model_df,
            duration_col=time_col,
            event_col=event_col,
            weights_col=weights_col,
            cluster_col=cluster_col,
            robust=True,
        )
    summ = cph.summary.loc["treatment"]
    ph_p = _ph_test(cph, model_df, time_col, event_col, weights_col, cluster_col)
    result = CoxResult(
        analysis=analysis,
        status="ok",
        **counts,
        hr=float(summ["exp(coef)"]),
        ci_low=float(summ["exp(coef) lower 95%"]),
        ci_high=float(summ["exp(coef) upper 95%"]),
        p_value=float(summ["p"]),
        ph_test_p=ph_p,
    )
    logger.info(
        "%s : HR %.3f [%.3f ; %.3f], événements %d / %d, test PH p=%.3f",
        analysis,
        result.hr,
        result.ci_low,
        result.ci_high,
        result.events_target,
        result.events_comparator,
        ph_p,
    )
    return result


def _ph_test(
    cph: CoxPHFitter,
    df: pd.DataFrame,
    time_col: str,
    event_col: str,
    weights_col: str | None,
    cluster_col: str | None,
) -> float:
    """p-valeur du test de Schoenfeld ; NaN si le test n'est pas calculable."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = proportional_hazard_test(cph, df, time_transform="rank")
        return float(np.asarray(res.p_value).ravel()[0])
    except Exception as exc:  # lifelines lève des erreurs variées selon le modèle
        logger.warning("Test PH non calculable : %s", exc)
        return float("nan")


def kaplan_meier(
    df: pd.DataFrame, time_col: str, event_col: str, weights_col: str | None = None
) -> pd.DataFrame:
    """Courbes de survie (éventuellement pondérées) par bras, format long."""
    frames = []
    for arm, label in ((1, "target"), (0, "comparator")):
        sub = df[df["treatment"] == arm]
        if weights_col is not None:
            sub = sub[sub[weights_col] > 0]
        kmf = KaplanMeierFitter()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            kmf.fit(
                sub[time_col],
                sub[event_col],
                weights=None if weights_col is None else sub[weights_col],
            )
        sf = kmf.survival_function_.iloc[:, 0]
        ci = kmf.confidence_interval_survival_function_
        frames.append(
            pd.DataFrame(
                {
                    "arm": label,
                    "time": sf.index.to_numpy(dtype=float),
                    "survival": sf.to_numpy(dtype=float),
                    "ci_low": ci.iloc[:, 0].to_numpy(dtype=float),
                    "ci_high": ci.iloc[:, 1].to_numpy(dtype=float),
                }
            )
        )
    return pd.concat(frames, ignore_index=True)
