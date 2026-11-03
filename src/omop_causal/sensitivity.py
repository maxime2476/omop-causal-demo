"""Analyses de sensibilité : E-value (VanderWeele & Ding 2017) et outils associés."""

import math
from dataclasses import asdict, dataclass


def evalue_rr(rr: float) -> float:
    """E-value d'un risque relatif : RR + sqrt(RR x (RR - 1)), avec RR inversé s'il est < 1.

    Raises:
        ValueError: si ``rr`` n'est pas strictement positif.
    """
    if not rr > 0:
        raise ValueError(f"RR doit être > 0 (reçu {rr})")
    r = rr if rr >= 1 else 1 / rr
    return r + math.sqrt(r * (r - 1))


def hr_to_rr(hr: float, rare_outcome: bool) -> float:
    """Approximation HR -> RR (VanderWeele 2017).

    - événement rare (incidence < ~15 % en fin de suivi) : RR ≈ HR ;
    - sinon : RR ≈ (1 - 0,5^sqrt(HR)) / (1 - 0,5^sqrt(1/HR)).
    """
    if not hr > 0:
        raise ValueError(f"HR doit être > 0 (reçu {hr})")
    if rare_outcome:
        return hr
    return float((1 - 0.5 ** math.sqrt(hr)) / (1 - 0.5 ** math.sqrt(1 / hr)))


@dataclass(frozen=True)
class EValueResult:
    """E-values du point et de la borne de l'IC la plus proche de 1."""

    hr: float
    ci_low: float
    ci_high: float
    outcome_risk: float
    rare_outcome: bool
    rr_point: float
    evalue_point: float
    evalue_ci: float

    def as_dict(self) -> dict[str, object]:
        """Représentation dictionnaire (export)."""
        return asdict(self)


def evalue_for_hr(
    hr: float, ci_low: float, ci_high: float, outcome_risk: float, rare_threshold: float
) -> EValueResult:
    """E-value d'un HR et de son IC 95 %.

    Pour l'IC : on prend la borne la plus proche de 1 ; si l'IC contient 1, l'E-value de
    l'IC vaut 1 (aucune confusion non mesurée n'est nécessaire pour l'expliquer).
    """
    rare = outcome_risk < rare_threshold
    rr = hr_to_rr(hr, rare)
    if ci_low <= 1 <= ci_high:
        e_ci = 1.0
    else:
        bound = ci_low if hr > 1 else ci_high
        e_ci = evalue_rr(hr_to_rr(bound, rare))
    return EValueResult(
        hr=hr,
        ci_low=ci_low,
        ci_high=ci_high,
        outcome_risk=outcome_risk,
        rare_outcome=rare,
        rr_point=rr,
        evalue_point=evalue_rr(rr),
        evalue_ci=e_ci,
    )
