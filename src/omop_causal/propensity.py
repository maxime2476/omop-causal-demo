"""Score de propension, appariement 1:1 sur le logit du PS et IPTW stabilisés tronqués."""

import logging
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd
from sklearn.linear_model import LogisticRegression, LogisticRegressionCV
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

_EPS = 1e-6


@dataclass(frozen=True)
class PropensityModel:
    """PS estimés et diagnostics du modèle."""

    ps: FloatArray
    logit: FloatArray
    best_c: float
    auc: float
    coefficients: pd.Series


def fit_propensity(
    X: pd.DataFrame,
    treatment: IntArray,
    c_grid: tuple[float, ...],
    cv_folds: int,
    seed: int,
) -> PropensityModel:
    """Régression logistique L2 (covariables standardisées), C choisi par validation croisée.

    Critère de sélection : log-vraisemblance (``neg_log_loss``), adapté à l'estimation de
    probabilités plutôt qu'à la classification.
    """
    model = make_pipeline(
        StandardScaler(),
        LogisticRegressionCV(
            Cs=list(c_grid),
            l1_ratios=(0.0,),
            cv=StratifiedKFold(n_splits=cv_folds, shuffle=True, random_state=seed),
            scoring="neg_log_loss",
            max_iter=10_000,
            use_legacy_attributes=False,
        ),
    )
    model.fit(X.to_numpy(dtype=float), treatment)
    ps = np.clip(model.predict_proba(X.to_numpy(dtype=float))[:, 1], _EPS, 1 - _EPS)
    lr = model[-1]
    best_c = float(lr.C_)
    auc = float(roc_auc_score(treatment, ps))
    coefs = pd.Series(lr.coef_.ravel(), index=X.columns, name="coef_standardized")
    logger.info("PS : C=%.4g retenu par CV, AUC=%.3f", best_c, auc)
    return PropensityModel(
        ps=ps, logit=np.log(ps / (1 - ps)), best_c=best_c, auc=auc, coefficients=coefs
    )


def fit_propensity_fixed_c(X: pd.DataFrame, treatment: IntArray, c: float) -> PropensityModel:
    """Même modèle que :func:`fit_propensity`, avec une valeur de C imposée (pas de CV)."""
    model = make_pipeline(StandardScaler(), LogisticRegression(C=c, max_iter=10_000))
    model.fit(X.to_numpy(dtype=float), treatment)
    ps = np.clip(model.predict_proba(X.to_numpy(dtype=float))[:, 1], _EPS, 1 - _EPS)
    coefs = pd.Series(model[-1].coef_.ravel(), index=X.columns, name="coef_standardized")
    return PropensityModel(
        ps=ps,
        logit=np.log(ps / (1 - ps)),
        best_c=c,
        auc=float(roc_auc_score(treatment, ps)),
        coefficients=coefs,
    )


def match_nearest_neighbor(
    logit_ps: FloatArray, treatment: IntArray, caliper_sd: float
) -> pd.DataFrame:
    """Appariement 1:1 sans remise au plus proche voisin sur le logit du PS, avec caliper.

    Caliper = ``caliper_sd`` x écart-type du logit du PS (échantillon complet, Austin 2011).
    Algorithme glouton global : toutes les paires admissibles sont triées par distance
    croissante et acceptées si ni le traité ni le témoin ne sont déjà utilisés ; chaque
    témoin est donc utilisé au plus une fois. Déterministe (ex aequo départagés par indice).

    Returns:
        DataFrame (``treated_idx``, ``control_idx``, ``distance``, ``pair_id``) ; les indices
        sont des positions dans les tableaux d'entrée.
    """
    caliper = caliper_sd * float(np.std(logit_ps, ddof=1))
    treated = np.flatnonzero(treatment == 1)
    controls = np.flatnonzero(treatment == 0)
    dist = np.abs(logit_ps[treated][:, None] - logit_ps[controls][None, :])
    ti, ci = np.nonzero(dist <= caliper)
    order = np.lexsort((ci, ti, dist[ti, ci]))
    used_t: set[int] = set()
    used_c: set[int] = set()
    pairs: list[tuple[int, int, float]] = []
    for k in order:
        t, c = int(treated[ti[k]]), int(controls[ci[k]])
        if t in used_t or c in used_c:
            continue
        used_t.add(t)
        used_c.add(c)
        pairs.append((t, c, float(dist[ti[k], ci[k]])))
    out = pd.DataFrame(pairs, columns=["treated_idx", "control_idx", "distance"])
    out["pair_id"] = np.arange(len(out))
    logger.info(
        "Appariement : %d paires (caliper %.4f sur le logit ; %d traités, %d témoins)",
        len(out),
        caliper,
        len(treated),
        len(controls),
    )
    return out


def stabilized_iptw(
    ps: FloatArray, treatment: IntArray, truncation: tuple[float, float] = (0.0, 100.0)
) -> FloatArray:
    """Poids IPTW stabilisés (estimand ATE), tronqués aux percentiles ``truncation``.

    w = P(T=1)/ps pour les traités, P(T=0)/(1-ps) pour les témoins ; les poids sont ensuite
    bornés (winsorisés) aux percentiles bas/haut de leur distribution.
    """
    p_treated = float(np.mean(treatment))
    w = np.where(treatment == 1, p_treated / ps, (1 - p_treated) / (1 - ps)).astype(float)
    low, high = truncation
    if (low, high) != (0.0, 100.0):
        lo_val, hi_val = np.percentile(w, [low, high])
        w = np.clip(w, lo_val, hi_val)
    return w


def att_odds_weights(
    ps: FloatArray, treatment: IntArray, truncation: tuple[float, float] = (0.0, 100.0)
) -> FloatArray:
    """Poids « odds » pour l'ATT : 1 pour les traités, ps/(1-ps) pour les témoins.

    Seuls les poids des témoins sont tronqués (percentiles de leur distribution).
    """
    w = np.where(treatment == 1, 1.0, ps / (1 - ps)).astype(float)
    low, high = truncation
    if (low, high) != (0.0, 100.0):
        ctrl = treatment == 0
        lo_val, hi_val = np.percentile(w[ctrl], [low, high])
        w[ctrl] = np.clip(w[ctrl], lo_val, hi_val)
    return w


def matched_sample_weights(n: int, pairs: pd.DataFrame) -> FloatArray:
    """Poids 0/1 indiquant l'appartenance à l'échantillon apparié."""
    w = np.zeros(n, dtype=float)
    w[pairs["treated_idx"].to_numpy()] = 1.0
    w[pairs["control_idx"].to_numpy()] = 1.0
    return w
