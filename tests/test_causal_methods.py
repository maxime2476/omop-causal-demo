"""SMD, appariement, IPTW, E-value et Cox sur des jeux jouets à valeur connue."""

import math

import numpy as np
import pandas as pd
import pytest

from omop_causal.balance import effective_sample_size, preference_score, smd
from omop_causal.outcomes import fit_cox
from omop_causal.propensity import att_odds_weights, match_nearest_neighbor, stabilized_iptw
from omop_causal.sensitivity import evalue_for_hr, evalue_rr, hr_to_rr


def test_smd_known_value_binary() -> None:
    # p_t = 0,5 ; p_c = 0,25 -> SMD = 0,25 / sqrt((0,25 + 0,1875) / 2)
    x = np.array([1, 1, 0, 0, 1, 0, 0, 0], dtype=float)
    t = np.array([1, 1, 1, 1, 0, 0, 0, 0])
    assert smd(x, t) == pytest.approx(0.25 / math.sqrt((0.25 + 0.1875) / 2))


def test_smd_known_value_continuous_and_weights() -> None:
    x = np.array([2.0, 4.0, 1.0, 3.0])
    t = np.array([1, 1, 0, 0])
    assert smd(x, t) == pytest.approx(1.0)  # moyennes 3 vs 2, variances 1 et 1
    # Pondération qui annule l'écart : témoin x=3 pèse lourd, traité x=4 nul.
    w = np.array([1.0, 0.0, 0.0, 1.0])
    assert smd(x, t, w, denominator_sd=1.0) == pytest.approx(-1.0)


def test_matching_never_reuses_a_control() -> None:
    rng = np.random.default_rng(0)
    logit = rng.normal(size=300)
    t = (rng.random(300) < 0.6).astype(np.int64)
    pairs = match_nearest_neighbor(logit, t, caliper_sd=0.2)
    assert pairs["control_idx"].is_unique
    assert pairs["treated_idx"].is_unique
    assert (t[pairs["treated_idx"]] == 1).all() and (t[pairs["control_idx"]] == 0).all()
    caliper = 0.2 * np.std(logit, ddof=1)
    assert (pairs["distance"] <= caliper + 1e-12).all()


def test_matching_respects_caliper_and_prefers_closest() -> None:
    logit = np.array([0.0, 10.0, 0.01, 0.5, 9.0])
    t = np.array([1, 1, 0, 0, 0])
    pairs = match_nearest_neighbor(logit, t, caliper_sd=0.1)  # caliper ≈ 0,1 x 4,9
    assert pairs[["treated_idx", "control_idx"]].values.tolist() == [[0, 2]]


def test_truncated_iptw_are_bounded() -> None:
    rng = np.random.default_rng(1)
    ps = np.clip(rng.beta(0.5, 0.5, 2000), 1e-4, 1 - 1e-4)
    t = (rng.random(2000) < ps).astype(np.int64)
    raw = stabilized_iptw(ps, t)
    w = stabilized_iptw(ps, t, (1, 99))
    lo, hi = np.percentile(raw, [1, 99])
    assert float(np.min(w)) >= lo - 1e-12 and float(np.max(w)) <= hi + 1e-12
    assert float(np.max(w)) < float(np.max(raw))
    att = att_odds_weights(ps, t, (1, 99))
    assert (att[t == 1] == 1).all()


def test_stabilized_weights_formula() -> None:
    ps = np.array([0.5, 0.25, 0.5, 0.75])
    t = np.array([1, 1, 0, 0])
    np.testing.assert_allclose(stabilized_iptw(ps, t), [1.0, 2.0, 1.0, 2.0])
    assert effective_sample_size(np.array([1.0, 1.0, 2.0])) == pytest.approx(16 / 6)


def test_preference_score_centred_on_prevalence() -> None:
    t = np.array([1, 1, 1, 0])
    assert preference_score(np.array([0.75]), t)[0] == pytest.approx(0.5)


def test_evalue_known_rr() -> None:
    assert evalue_rr(2.0) == pytest.approx(2 + math.sqrt(2))
    assert evalue_rr(0.5) == pytest.approx(2 + math.sqrt(2))
    assert evalue_rr(1.0) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        evalue_rr(0.0)


def test_evalue_for_hr_rare_and_common() -> None:
    rare = evalue_for_hr(2.0, 1.5, 2.7, outcome_risk=0.05, rare_threshold=0.15)
    assert rare.rare_outcome and rare.rr_point == 2.0
    assert rare.evalue_ci == pytest.approx(evalue_rr(1.5))
    common = evalue_for_hr(2.0, 0.9, 4.0, outcome_risk=0.3, rare_threshold=0.15)
    expected_rr = (1 - 0.5 ** math.sqrt(2)) / (1 - 0.5 ** math.sqrt(0.5))
    assert common.rr_point == pytest.approx(expected_rr)
    assert common.evalue_ci == 1.0  # IC contenant 1
    assert hr_to_rr(1.0, rare_outcome=False) == pytest.approx(1.0)


def test_cox_reports_insufficient_events() -> None:
    df = pd.DataFrame(
        {"treatment": [1, 1, 0, 0], "time": [10.0, 20, 30, 40], "event": [1, 0, 0, 0]}
    )
    res = fit_cox(df, "x|itt", "time", "event", min_events_per_arm=1)
    assert res.status == "insufficient_events" and math.isnan(res.hr)


def test_cox_recovers_hazard_ratio() -> None:
    rng = np.random.default_rng(3)
    n = 4000
    t = rng.integers(0, 2, n)
    time = rng.exponential(1 / (0.01 * np.where(t == 1, 2.0, 1.0)))
    df = pd.DataFrame(
        {"treatment": t, "time": np.minimum(time, 100), "event": (time < 100).astype(int)}
    )
    res = fit_cox(df, "x|itt", "time", "event", min_events_per_arm=10)
    assert res.status == "ok"
    assert res.ci_low < 2.0 < res.ci_high
