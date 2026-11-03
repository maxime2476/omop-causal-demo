"""Diagnostics d'équilibre : SMD, score de préférence, taille d'échantillon effective."""

import numpy as np
import numpy.typing as npt
import pandas as pd

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]


def _weighted_mean_var(x: FloatArray, w: FloatArray) -> tuple[float, float]:
    mean = float(np.average(x, weights=w))
    var = float(np.average((x - mean) ** 2, weights=w))
    return mean, var


def smd(
    x: FloatArray,
    treatment: IntArray,
    weights: FloatArray | None = None,
    denominator_sd: float | None = None,
) -> float:
    """Différence moyenne standardisée (traités - témoins).

    Dénominateur : ``denominator_sd`` s'il est fourni (convention : écart-type poolé de
    l'échantillon NON ajusté, pour que le SMD post-ajustement ne baisse pas par simple
    inflation de variance), sinon sqrt((var_t + var_c) / 2) dans l'échantillon considéré.
    Valable pour variables continues et binaires (variance p(1-p)).
    """
    w = np.ones_like(x, dtype=float) if weights is None else weights
    t, c = treatment == 1, treatment == 0
    if w[t].sum() <= 0 or w[c].sum() <= 0:
        return float("nan")
    m_t, v_t = _weighted_mean_var(x[t], w[t])
    m_c, v_c = _weighted_mean_var(x[c], w[c])
    sd = denominator_sd if denominator_sd is not None else float(np.sqrt((v_t + v_c) / 2))
    if sd == 0:
        return 0.0 if m_t == m_c else float("inf")
    return (m_t - m_c) / sd


def pooled_sd(x: FloatArray, treatment: IntArray) -> float:
    """Écart-type poolé non pondéré sqrt((var_t + var_c) / 2)."""
    return float(np.sqrt((np.var(x[treatment == 1]) + np.var(x[treatment == 0])) / 2))


def balance_table(
    X: pd.DataFrame, treatment: IntArray, samples: dict[str, FloatArray]
) -> pd.DataFrame:
    """Table d'équilibre : moyennes par bras et SMD pour chaque échantillon pondéré.

    ``samples`` associe un nom (ex. ``"before"``, ``"matched"``, ``"iptw"``) à un vecteur
    de poids (1 partout pour l'échantillon brut, 0/1 pour l'apparié).
    """
    rows = []
    for col in X.columns:
        x = X[col].to_numpy(dtype=float)
        denom = pooled_sd(x, treatment)
        row: dict[str, float | str] = {"covariate": col}
        for name, w in samples.items():
            t, c = treatment == 1, treatment == 0
            row[f"mean_target_{name}"] = float(np.average(x[t], weights=w[t]))
            row[f"mean_comparator_{name}"] = float(np.average(x[c], weights=w[c]))
            row[f"smd_{name}"] = smd(x, treatment, w, denominator_sd=denom if denom > 0 else None)
        rows.append(row)
    return pd.DataFrame(rows)


def effective_sample_size(weights: FloatArray) -> float:
    """Taille d'échantillon effective de Kish : (somme w)^2 / somme w^2."""
    return float(weights.sum() ** 2 / np.square(weights).sum())


def preference_score(ps: FloatArray, treatment: IntArray) -> FloatArray:
    """Score de préférence (Walker et al. 2013) : PS recentré sur la prévalence du traitement."""
    p = float(np.mean(treatment))
    logit_f = np.log(ps / (1 - ps)) - np.log(p / (1 - p))
    return np.asarray(1 / (1 + np.exp(-logit_f)), dtype=np.float64)


def equipoise_fraction(pref: FloatArray, low: float = 0.3, high: float = 0.7) -> float:
    """Part des patients en équipoise (score de préférence dans [0,3 ; 0,7])."""
    return float(np.mean((pref >= low) & (pref <= high)))
