"""Orchestration : cohortes -> covariables -> PS -> équilibre -> effets -> sensibilité."""

import logging
from dataclasses import dataclass, field

import duckdb
import numpy as np
import pandas as pd
from lifelines import KaplanMeierFitter

from omop_causal.balance import (
    balance_table,
    effective_sample_size,
    equipoise_fraction,
    preference_score,
)
from omop_causal.cohorts import (
    CohortResult,
    apply_eligibility,
    follow_up,
    has_outcome_before,
    load_candidates,
    load_outcome_dates,
)
from omop_causal.concepts import ResolvedConcept, resolve_concept
from omop_causal.config import ConceptSpec, StudyConfig
from omop_causal.covariates import Covariates, build_covariates
from omop_causal.outcomes import CoxResult, fit_cox, kaplan_meier
from omop_causal.propensity import (
    PropensityModel,
    att_odds_weights,
    fit_propensity,
    fit_propensity_fixed_c,
    match_nearest_neighbor,
    matched_sample_weights,
    stabilized_iptw,
)
from omop_causal.sensitivity import EValueResult, evalue_for_hr

logger = logging.getLogger(__name__)

ANALYSIS_LABELS = {
    "unadjusted": "Non ajusté",
    "matched": "Appariement PS 1:1 (ATT*)",
    "iptw_ate": "IPTW stabilisé (ATE)",
    "iptw_att": "Pondération odds (ATT)",
    "iptw_ate_tuned": "IPTW, C choisi sur l'équilibre (post-hoc)",
}


@dataclass
class StudyResults:
    """Tous les résultats nécessaires au rapport."""

    concepts: list[ResolvedConcept]
    cohort: CohortResult
    data: pd.DataFrame  # cohorte + suivi + PS + poids
    covariates: Covariates
    ps_model: PropensityModel
    pairs: pd.DataFrame
    balance: pd.DataFrame
    balance_summary: pd.DataFrame
    effects: pd.DataFrame
    km: pd.DataFrame
    evalue: EValueResult | None
    caliper_sensitivity: pd.DataFrame
    truncation_sensitivity: pd.DataFrame
    negative_controls: pd.DataFrame
    balance_tuning: pd.DataFrame
    notes: list[str] = field(default_factory=list)


def matched_frame(df: pd.DataFrame, pairs: pd.DataFrame) -> pd.DataFrame:
    """Échantillon apparié long avec identifiant de paire."""
    treated = df.iloc[pairs["treated_idx"].to_numpy()].assign(pair_id=pairs["pair_id"].to_numpy())
    control = df.iloc[pairs["control_idx"].to_numpy()].assign(pair_id=pairs["pair_id"].to_numpy())
    return pd.concat([treated, control], ignore_index=True)


def estimate_effects(
    df: pd.DataFrame,
    pairs: pd.DataFrame,
    time_col: str,
    event_col: str,
    follow_up_label: str,
    min_events: int,
) -> list[CoxResult]:
    """HR non ajusté, apparié, IPTW (ATE) et pondéré ATT pour une définition de suivi.

    ``df`` doit contenir les colonnes ``iptw`` et ``att_w``.
    """

    def name(key: str) -> str:
        return f"{key}|{follow_up_label}"

    return [
        fit_cox(df, name("unadjusted"), time_col, event_col, min_events),
        fit_cox(
            matched_frame(df, pairs),
            name("matched"),
            time_col,
            event_col,
            min_events,
            cluster_col="pair_id",
        ),
        fit_cox(df, name("iptw_ate"), time_col, event_col, min_events, weights_col="iptw"),
        fit_cox(df, name("iptw_att"), time_col, event_col, min_events, weights_col="att_w"),
    ]


def _effects_frame(results: list[CoxResult]) -> pd.DataFrame:
    out = pd.DataFrame([r.as_dict() for r in results])
    parts = out["analysis"].str.split("|", expand=True)
    out.insert(1, "method", parts[0])
    out.insert(2, "follow_up", parts[1])
    out.insert(3, "label", out["method"].map(ANALYSIS_LABELS))
    return out


def _max_abs_smd(balance: pd.DataFrame, sample: str) -> float:
    return float(balance[f"smd_{sample}"].abs().max())


def _balance_summary(
    df: pd.DataFrame, balance: pd.DataFrame, samples: dict[str, np.ndarray], threshold: float
) -> pd.DataFrame:
    t = df["treatment"].to_numpy() == 1
    rows = []
    for name, w in samples.items():
        smds = balance[f"smd_{name}"].abs()
        rows.append(
            {
                "sample": name,
                "n_target": int((w[t] > 0).sum()),
                "n_comparator": int((w[~t] > 0).sum()),
                "ess_target": effective_sample_size(w[t][w[t] > 0]),
                "ess_comparator": effective_sample_size(w[~t][w[~t] > 0]),
                "max_abs_smd": float(smds.max()),
                "n_covariates": len(smds),
                "n_smd_above_threshold": int((smds >= threshold).sum()),
            }
        )
    return pd.DataFrame(rows)


def _outcome_risk(df: pd.DataFrame, horizon: int) -> float:
    """Incidence cumulée Kaplan-Meier du critère à l'horizon ITT (cohorte entière)."""
    kmf = KaplanMeierFitter().fit(df["time_itt"], df["event_itt"])
    return float(1 - kmf.predict(horizon))


def _caliper_sensitivity(df: pd.DataFrame, X: pd.DataFrame, cfg: StudyConfig) -> pd.DataFrame:
    t = df["treatment"].to_numpy()
    rows = []
    for cal in cfg.sensitivity.calipers:
        pairs = match_nearest_neighbor(df["logit_ps"].to_numpy(), t, cal)
        w = matched_sample_weights(len(df), pairs)
        bal = balance_table(X, t, {"m": w})
        res = fit_cox(
            matched_frame(df, pairs),
            f"matched|caliper={cal}",
            "time_itt",
            "event_itt",
            cfg.min_events_per_arm,
            cluster_col="pair_id",
        )
        rows.append(
            {"caliper_sd": cal, "n_pairs": len(pairs), "max_abs_smd": _max_abs_smd(bal, "m")}
            | {k: v for k, v in res.as_dict().items() if k != "analysis"}
        )
    return pd.DataFrame(rows)


def _truncation_sensitivity(df: pd.DataFrame, X: pd.DataFrame, cfg: StudyConfig) -> pd.DataFrame:
    t = df["treatment"].to_numpy()
    ps = df["ps"].to_numpy()
    rows = []
    for low, high in cfg.sensitivity.truncations:
        w = stabilized_iptw(ps, t, (low, high))
        bal = balance_table(X, t, {"w": w})
        res = fit_cox(
            df.assign(iptw=w),
            f"iptw_ate|trunc={low}-{high}",
            "time_itt",
            "event_itt",
            cfg.min_events_per_arm,
            weights_col="iptw",
        )
        rows.append(
            {
                "truncation": f"{low:g}-{high:g}",
                "max_weight": float(np.max(w)),
                "ess": effective_sample_size(w),
                "max_abs_smd": _max_abs_smd(bal, "w"),
            }
            | {k: v for k, v in res.as_dict().items() if k != "analysis"}
        )
    return pd.DataFrame(rows)


def balance_tuned_iptw(
    X: pd.DataFrame, treatment: np.ndarray, cfg: StudyConfig
) -> tuple[pd.DataFrame, np.ndarray, float]:
    """Sensibilité POST-HOC : C choisi pour minimiser le max |SMD| après IPTW.

    Sélection fondée uniquement sur l'équilibre des covariables (étape de design), sans
    utiliser le critère de jugement. Retourne le diagnostic par C, les poids retenus et C.
    """
    trunc = cfg.propensity.iptw_truncation_percentiles
    rows, weights = [], {}
    for c in cfg.propensity.c_grid:
        model = fit_propensity_fixed_c(X, treatment, c)
        w = stabilized_iptw(model.ps, treatment, trunc)
        bal = balance_table(X, treatment, {"w": w})
        weights[c] = w
        rows.append(
            {
                "c": c,
                "auc": model.auc,
                "max_abs_smd": _max_abs_smd(bal, "w"),
                "n_smd_above_threshold": int((bal["smd_w"].abs() >= cfg.smd_threshold).sum()),
                "ess": effective_sample_size(w),
                "max_weight": float(np.max(w)),
            }
        )
    table = pd.DataFrame(rows)
    best = float(table["c"].iloc[int(np.argmin(table["max_abs_smd"].to_numpy()))])
    table["selected"] = table["c"] == best
    logger.info("IPTW post-hoc : C=%g retenu (max |SMD| minimal)", best)
    return table, weights[best], best


def _resolve_by_id(con: duckdb.DuckDBPyConnection, concept_id: int, label: str) -> ResolvedConcept:
    """Vérifie qu'un concept_id de contrôle négatif existe et est standard, puis le résout."""
    row = con.execute(
        "SELECT concept_name, vocabulary_id, concept_class_id FROM concept "
        "WHERE concept_id = $id AND standard_concept = 'S'",
        {"id": concept_id},
    ).fetchone()
    if row is None:
        raise LookupError(f"Contrôle négatif {label} : concept {concept_id} absent ou non standard")
    spec = ConceptSpec(label, str(row[0]), str(row[1]), str(row[2]), concept_id)
    return resolve_concept(con, spec)


def _negative_controls(
    con: duckdb.DuckDBPyConnection, df: pd.DataFrame, pairs: pd.DataFrame, cfg: StudyConfig
) -> pd.DataFrame:
    """HR (IPTW et appariement) pour chaque contrôle négatif validé.

    Les PS/poids/paires de l'analyse principale sont réutilisés ; les personnes ayant le
    contrôle dans la fenêtre ``[t0 - lookback, t0]`` sont exclues (paires incomplètes retirées).
    """
    rows = []
    for nc in cfg.sensitivity.negative_controls:
        concept = _resolve_by_id(con, nc.concept_id, nc.label)
        dates = load_outcome_dates(con, concept.descendant_ids)
        prior = has_outcome_before(df, dates, cfg.sensitivity.negative_control_prior_lookback_days)
        nc_df = follow_up(df.loc[~prior].reset_index(drop=True), dates, cfg.follow_up)
        kept = set(df.index[~prior])
        nc_pairs = pairs[pairs["treated_idx"].isin(kept) & pairs["control_idx"].isin(kept)]
        remap = {old: new for new, old in enumerate(df.index[~prior])}
        nc_pairs = nc_pairs.assign(
            treated_idx=nc_pairs["treated_idx"].map(remap),
            control_idx=nc_pairs["control_idx"].map(remap),
        )
        for res in (
            fit_cox(
                nc_df,
                f"iptw_ate|{nc.label}",
                "time_itt",
                "event_itt",
                cfg.min_events_per_arm,
                weights_col="iptw",
            ),
            fit_cox(
                nc_df,
                f"iptw_ate_tuned|{nc.label}",
                "time_itt",
                "event_itt",
                cfg.min_events_per_arm,
                weights_col="iptw_tuned",
            ),
            fit_cox(
                matched_frame(nc_df, nc_pairs),
                f"matched|{nc.label}",
                "time_itt",
                "event_itt",
                cfg.min_events_per_arm,
                cluster_col="pair_id",
            ),
        ):
            method, _ = res.analysis.split("|")
            rows.append(
                {
                    "concept_id": nc.concept_id,
                    "label": nc.label,
                    "method": method,
                    "n_excluded_prior": int(prior.sum()),
                }
                | {k: v for k, v in res.as_dict().items() if k != "analysis"}
            )
    return pd.DataFrame(rows)


def run_study(con: duckdb.DuckDBPyConnection, cfg: StudyConfig) -> StudyResults:
    """Exécute l'étude complète (sans écriture disque)."""
    notes: list[str] = []
    target, comparator, outcome = (
        resolve_concept(con, s) for s in (cfg.target, cfg.comparator, cfg.outcome)
    )
    candidates = load_candidates(con, target.descendant_ids, comparator.descendant_ids)
    outcome_dates = load_outcome_dates(con, outcome.descendant_ids)
    cohort = apply_eligibility(candidates, outcome_dates, cfg.cohort, cfg.outcome.label)
    df = follow_up(cohort.cohort, outcome_dates, cfg.follow_up)

    exposure_days = (df["exposure_end_date"] - df["t0"]).dt.days
    if (exposure_days == 0).all():
        notes.append(
            "Toutes les expositions index (drug_era / drug_exposure) durent 0 jour dans la "
            "base : l'analyse « on-treatment » revient à une fenêtre fixe de "
            f"{cfg.follow_up.on_treatment_grace_days} jours après t0."
        )

    cov = build_covariates(
        con, df, target.descendant_ids + comparator.descendant_ids, cfg.covariates
    )
    X = cov.X.loc[df["person_id"]].reset_index(drop=True)
    t = df["treatment"].to_numpy()

    ps_model = fit_propensity(X, t, cfg.propensity.c_grid, cfg.propensity.cv_folds, cfg.seed)
    df["ps"] = ps_model.ps
    df["logit_ps"] = ps_model.logit
    df["preference"] = preference_score(ps_model.ps, t)
    trunc = cfg.propensity.iptw_truncation_percentiles
    df["iptw"] = stabilized_iptw(ps_model.ps, t, trunc)
    df["att_w"] = att_odds_weights(ps_model.ps, t, trunc)
    pairs = match_nearest_neighbor(ps_model.logit, t, cfg.propensity.caliper_sd)
    n_treated, n_ctrl = int(t.sum()), int((1 - t).sum())
    if len(pairs) < n_treated:
        notes.append(
            f"Appariement : {len(pairs)} paires sur {n_treated} traités ({n_ctrl} témoins "
            "disponibles). Les traités non appariés sont exclus : l'estimand de l'analyse "
            "appariée est l'effet chez les traités appariés (« ATT* »), pas l'ATT strict ; "
            "la pondération odds (iptw_att) estime l'ATT sur tous les traités."
        )

    tuning, tuned_w, tuned_c = balance_tuned_iptw(X, t, cfg)
    df["iptw_tuned"] = tuned_w
    notes.append(
        f"Sensibilité post-hoc (ajoutée après lecture des HR, pour le déséquilibre IPTW) : "
        f"C = {tuned_c:g} choisi sur l'équilibre seul ; l'analyse principale est inchangée."
    )

    samples = {
        "before": np.ones(len(df)),
        "matched": matched_sample_weights(len(df), pairs),
        "iptw": df["iptw"].to_numpy(),
        "att_weighted": df["att_w"].to_numpy(),
        "iptw_tuned": tuned_w,
    }
    balance = balance_table(X, t, samples).merge(cov.labels, on="covariate", how="left")
    balance_summary = _balance_summary(df, balance, samples, cfg.smd_threshold)
    for sample in ("matched", "iptw", "att_weighted", "iptw_tuned"):
        n_bad = int((balance[f"smd_{sample}"].abs() >= cfg.smd_threshold).sum())
        if n_bad:
            notes.append(
                f"Équilibre {sample} : {n_bad} covariable(s) avec |SMD| >= {cfg.smd_threshold}."
            )

    results = estimate_effects(df, pairs, "time_itt", "event_itt", "itt", cfg.min_events_per_arm)
    results += estimate_effects(
        df, pairs, "time_ot", "event_ot", "on_treatment", cfg.min_events_per_arm
    )
    for time_col, event_col, label in (
        ("time_itt", "event_itt", "itt"),
        ("time_ot", "event_ot", "on_treatment"),
    ):
        results.append(
            fit_cox(
                df,
                f"iptw_ate_tuned|{label}",
                time_col,
                event_col,
                cfg.min_events_per_arm,
                weights_col="iptw_tuned",
            )
        )
    effects = _effects_frame(results)
    effects["primary"] = (effects["method"] == cfg.primary_analysis) & (
        effects["follow_up"] == "itt"
    )

    km = pd.concat(
        [
            kaplan_meier(df, "time_itt", "event_itt").assign(sample="unadjusted"),
            kaplan_meier(matched_frame(df, pairs), "time_itt", "event_itt").assign(
                sample="matched"
            ),
            kaplan_meier(df, "time_itt", "event_itt", weights_col="iptw").assign(sample="iptw"),
            kaplan_meier(df, "time_itt", "event_itt", weights_col="iptw_tuned").assign(
                sample="iptw_tuned"
            ),
        ],
        ignore_index=True,
    )

    primary = effects[effects["primary"]].iloc[0]
    evalue = None
    if primary["status"] == "ok":
        evalue = evalue_for_hr(
            float(primary["hr"]),
            float(primary["ci_low"]),
            float(primary["ci_high"]),
            _outcome_risk(df, cfg.follow_up.itt_days),
            cfg.sensitivity.rare_outcome_threshold,
        )
    else:
        notes.append("Analyse principale non estimable (événements insuffisants) : pas d'E-value.")

    if cfg.sensitivity.negative_controls:
        nc = _negative_controls(con, df, pairs, cfg)
    else:
        nc = pd.DataFrame()
        notes.append("Contrôles négatifs : liste en attente de validation, non estimés.")

    return StudyResults(
        concepts=[target, comparator, outcome],
        cohort=cohort,
        data=df,
        covariates=cov,
        ps_model=ps_model,
        pairs=pairs,
        balance=balance,
        balance_summary=balance_summary,
        effects=effects,
        km=km,
        evalue=evalue,
        caliper_sensitivity=_caliper_sensitivity(df, X, cfg),
        truncation_sensitivity=_truncation_sensitivity(df, X, cfg),
        negative_controls=nc,
        balance_tuning=tuning,
        notes=notes,
    )


def ps_diagnostics(results: StudyResults) -> dict[str, float]:
    """Indicateurs du modèle de PS (C, AUC, part en équipoise)."""
    return {
        "best_c": results.ps_model.best_c,
        "auc": results.ps_model.auc,
        "equipoise_fraction": equipoise_fraction(results.data["preference"].to_numpy()),
    }
