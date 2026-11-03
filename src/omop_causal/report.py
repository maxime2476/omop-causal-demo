"""Écriture des tables, figures et du résumé ``output/report/``."""

import json
import logging
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator

from omop_causal.concepts import concepts_table
from omop_causal.config import StudyConfig
from omop_causal.study import ANALYSIS_LABELS, StudyResults, ps_diagnostics

logger = logging.getLogger(__name__)

# Palette catégorielle de référence (ordre fixe, validée CVD) et encres de texte.
TARGET_COLOR = "#2a78d6"
COMPARATOR_COLOR = "#eb6834"
SAMPLE_COLORS = {
    "before": "#8a8984",
    "matched": "#2a78d6",
    "iptw": "#eb6834",
    "iptw_tuned": "#1baf7a",
}
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
SURFACE = "#fcfcfb"

plt.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "axes.edgecolor": INK_2,
        "axes.labelcolor": INK,
        "axes.titlecolor": INK,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "xtick.color": INK_2,
        "ytick.color": INK_2,
        "font.size": 9,
        "legend.frameon": False,
        "savefig.dpi": 150,
        "savefig.bbox": "tight",
    }
)


def _fmt(x: Any, nd: int = 2) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "NA"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _md_table(df: pd.DataFrame, nd: int = 3) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(_fmt(row[c], nd) for c in cols) + " |")
    return "\n".join(lines)


def _labels(cfg: StudyConfig) -> tuple[str, str]:
    return cfg.target.label, cfg.comparator.label


# --------------------------------------------------------------------------- figures


def plot_attrition(attrition: pd.DataFrame, cfg: StudyConfig, path: Path) -> None:
    """Flowchart d'attrition (une boîte par critère)."""
    t_lab, c_lab = _labels(cfg)
    n = len(attrition)
    fig, ax = plt.subplots(figsize=(7.5, 1.1 * n + 0.4))
    ax.set_axis_off()
    ax.grid(False)
    for i, row in attrition.reset_index(drop=True).iterrows():
        y = n - 1 - int(str(i))
        text = (
            f"{row['step']}\n"
            f"n = {row['n_total']}  ({t_lab} {row['n_target']} · {c_lab} {row['n_comparator']})"
        )
        ax.text(
            0.5,
            y,
            text,
            ha="center",
            va="center",
            color=INK,
            fontsize=9,
            bbox={"boxstyle": "round,pad=0.5", "fc": "white", "ec": INK_2, "lw": 0.8},
        )
        if int(str(i)) > 0 and row["n_excluded"] > 0:
            ax.text(
                0.93,
                y + 0.5,
                f"exclus : {row['n_excluded']}",
                ha="left",
                va="center",
                color=INK_2,
                fontsize=8,
            )
        if int(str(i)) < n - 1:
            ax.annotate(
                "",
                xy=(0.5, y - 0.68),
                xytext=(0.5, y - 0.32),
                arrowprops={"arrowstyle": "->", "color": INK_2, "lw": 1},
            )
    ax.set_xlim(0, 1.15)
    ax.set_ylim(-0.6, n - 0.4)
    ax.set_title("Flowchart d'attrition", loc="left", fontsize=11)
    fig.savefig(path)
    plt.close(fig)


def _density(ax: Axes, values: np.ndarray, color: str, label: str) -> None:
    bins = np.linspace(0, 1, 41)
    hist, edges = np.histogram(values, bins=bins, density=True)
    centers = (edges[:-1] + edges[1:]) / 2
    ax.fill_between(centers, hist, color=color, alpha=0.25, step="mid", linewidth=0)
    ax.step(centers, hist, where="mid", color=color, lw=2, label=label)


def plot_preference(results: StudyResults, cfg: StudyConfig, path: Path) -> None:
    """Distribution du score de préférence par bras, avant et après appariement."""
    t_lab, c_lab = _labels(cfg)
    df = results.data
    matched_idx = np.concatenate(
        [results.pairs["treated_idx"].to_numpy(), results.pairs["control_idx"].to_numpy()]
    )
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.4), sharey=True)
    for ax, sub, title in (
        (axes[0], df, "Cohorte complète"),
        (axes[1], df.iloc[matched_idx], "Échantillon apparié"),
    ):
        _density(ax, sub.loc[sub.treatment == 1, "preference"].to_numpy(), TARGET_COLOR, t_lab)
        _density(ax, sub.loc[sub.treatment == 0, "preference"].to_numpy(), COMPARATOR_COLOR, c_lab)
        ax.axvspan(0.3, 0.7, color=GRID, alpha=0.5, lw=0, zorder=0)
        ax.set_title(title, loc="left")
        ax.set_xlabel("Score de préférence")
        ax.set_xlim(0, 1)
    axes[0].set_ylabel("Densité")
    axes[0].legend(loc="upper left")
    eq = ps_diagnostics(results)["equipoise_fraction"]
    fig.suptitle(
        f"Score de préférence — zone grisée : équipoise [0,3 ; 0,7] ({eq:.1%} de la cohorte)",
        x=0.01,
        ha="left",
        fontsize=10,
        color=INK,
    )
    fig.savefig(path)
    plt.close(fig)


def plot_love(balance: pd.DataFrame, threshold: float, path: Path) -> None:
    """Love plot : |SMD| par covariable avant et après ajustement."""
    b = balance.assign(abs_before=balance["smd_before"].abs()).sort_values("abs_before")
    y = np.arange(len(b))
    fig, ax = plt.subplots(figsize=(8, 0.17 * len(b) + 1.4))
    for sample, marker, name in (
        ("before", "o", "Avant ajustement"),
        ("matched", "s", "Apparié"),
        ("iptw", "D", "IPTW (ATE)"),
        ("iptw_tuned", "^", "IPTW, C sur l'équilibre (post-hoc)"),
    ):
        ax.scatter(
            b[f"smd_{sample}"].abs(),
            y,
            s=22,
            marker=marker,
            color=SAMPLE_COLORS[sample],
            edgecolors=SURFACE,
            linewidths=0.8,
            label=name,
            zorder=3,
        )
    ax.axvline(threshold, color=INK_2, ls="--", lw=1)
    ax.text(threshold, len(b) - 0.2, f" seuil {threshold}", color=INK_2, fontsize=8, va="bottom")
    ax.set_yticks(y)
    ax.set_yticklabels(b["label"], fontsize=6.5)
    ax.set_ylim(-0.8, len(b) + 0.4)
    ax.set_xlabel("|Différence moyenne standardisée|")
    ax.set_title("Équilibre des covariables (love plot)", loc="left", fontsize=11)
    ax.legend(loc="lower right")
    fig.savefig(path)
    plt.close(fig)


def plot_km(km: pd.DataFrame, cfg: StudyConfig, path: Path) -> None:
    """Incidence cumulée (1 - KM) par bras : non ajusté, apparié, IPTW."""
    t_lab, c_lab = _labels(cfg)
    titles = {
        "unadjusted": "Non ajusté",
        "matched": "Apparié",
        "iptw": "IPTW (ATE)",
        "iptw_tuned": "IPTW, C sur l'équilibre (post-hoc)",
    }
    fig, axes = plt.subplots(1, 4, figsize=(14, 3.4), sharey=True)
    for ax, (sample, title) in zip(axes, titles.items(), strict=True):
        sub = km[km["sample"] == sample]
        for arm, color, lab in (
            ("target", TARGET_COLOR, t_lab),
            ("comparator", COMPARATOR_COLOR, c_lab),
        ):
            s = sub[sub["arm"] == arm]
            ax.step(s["time"], 1 - s["survival"], where="post", color=color, lw=2, label=lab)
            ax.fill_between(
                s["time"],
                1 - s["ci_high"],
                1 - s["ci_low"],
                step="post",
                color=color,
                alpha=0.15,
                lw=0,
            )
        ax.set_title(title, loc="left")
        ax.set_xlabel("Jours depuis t0")
        ax.set_xlim(0, cfg.follow_up.itt_days)
    axes[0].set_ylabel(f"Incidence cumulée — {cfg.outcome.label}")
    axes[0].legend(loc="upper left")
    fig.savefig(path)
    plt.close(fig)


def _forest(ax: Axes, df: pd.DataFrame, labels: list[str]) -> None:
    y = np.arange(len(df))[::-1]
    ok = df["status"] == "ok"
    ax.errorbar(
        df.loc[ok, "hr"],
        y[ok.to_numpy()],
        xerr=[df.loc[ok, "hr"] - df.loc[ok, "ci_low"], df.loc[ok, "ci_high"] - df.loc[ok, "hr"]],
        fmt="o",
        color=TARGET_COLOR,
        ecolor=INK_2,
        elinewidth=1.2,
        capsize=0,
        ms=6,
    )
    for yy in y[~ok.to_numpy()]:
        ax.text(
            0.01,
            yy,
            "événements insuffisants",
            ha="left",
            va="center",
            color=INK_2,
            fontsize=8,
            transform=ax.get_yaxis_transform(),
        )
    ax.axvline(1, color=INK_2, lw=1)
    ax.set_xscale("log")
    ax.xaxis.set_major_locator(FixedLocator([0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4]))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.set_xlabel("Hazard ratio (IC 95 %, échelle log)")


def plot_effects(effects: pd.DataFrame, cfg: StudyConfig, path: Path) -> None:
    """Forest plot des HR (toutes analyses)."""
    fu = {"itt": "ITT 365 j", "on_treatment": "On-treatment"}
    labels = [
        f"{ANALYSIS_LABELS[m]} — {fu[f]}"
        for m, f in zip(effects["method"], effects["follow_up"], strict=True)
    ]
    fig, ax = plt.subplots(figsize=(8, 0.4 * len(effects) + 1.2))
    _forest(ax, effects, labels)
    ax.set_title(
        f"{cfg.target.label} vs {cfg.comparator.label} — {cfg.outcome.label}",
        loc="left",
        fontsize=11,
    )
    fig.savefig(path)
    plt.close(fig)


def plot_negative_controls(nc: pd.DataFrame, path: Path) -> None:
    """Forest plot des HR des contrôles négatifs estimables (attendus ≈ 1).

    Les contrôles sans assez d'événements sont listés en note, pas tracés.
    """
    ok = nc[nc["status"] == "ok"].reset_index(drop=True)
    skipped = sorted(set(nc.loc[nc["status"] != "ok", "label"]))
    labels = [f"{r.label} ({r.method})" for r in ok.itertuples()]
    fig, ax = plt.subplots(figsize=(8, 0.35 * max(len(ok), 1) + 1.6))
    if not ok.empty:
        _forest(ax, ok, labels)
    ax.set_title("Contrôles négatifs (HR attendu ≈ 1)", loc="left", fontsize=11)
    if skipped:
        fig.text(
            0.01,
            -0.02,
            "Non estimés (< seuil d'événements par bras) : " + ", ".join(skipped),
            ha="left",
            va="top",
            fontsize=7.5,
            color=INK_2,
            wrap=True,
        )
    fig.savefig(path)
    plt.close(fig)


# --------------------------------------------------------------------------- tables


def table1(results: StudyResults, cfg: StudyConfig) -> pd.DataFrame:
    """Table 1 : moyennes/proportions par bras et SMD, avant et après ajustement."""
    b = results.balance
    cols = ["label", "kind"]
    for s in ("before", "matched", "iptw", "iptw_tuned"):
        cols += [f"mean_target_{s}", f"mean_comparator_{s}", f"smd_{s}"]
    out = b[cols].rename(
        columns=lambda c: c.replace("target", cfg.target.label).replace(
            "comparator", cfg.comparator.label
        )
    )
    return out


COHORT_EXPORT_COLUMNS = (
    "person_id",
    "arm",
    "t0",
    "exposure_end_date",
    "obs_end_date",
    "time_itt",
    "event_itt",
    "ps",
)


def cohort_export(data: pd.DataFrame) -> pd.DataFrame:
    """Cohorte éligible (une ligne par personne), relue par ``r/04_cohortmethod_crosscheck.R``.

    Seuls des identifiants de la base simulée et des dates sont exportés, dans ``output/``.
    """
    cols = [c for c in COHORT_EXPORT_COLUMNS if c in data.columns]
    out = data[cols].copy()
    for col in ("t0", "exposure_end_date", "obs_end_date"):
        if col in out.columns:
            out[col] = pd.to_datetime(out[col]).dt.date
    return out.sort_values("person_id").reset_index(drop=True)


def write_report(results: StudyResults, cfg: StudyConfig) -> list[Path]:
    """Écrit toutes les sorties ; retourne la liste des fichiers créés."""
    rep = cfg.report_dir
    fig_dir = rep / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def csv(df: pd.DataFrame, name: str) -> None:
        path = rep / name
        df.to_csv(path, index=False)
        written.append(path)

    csv(concepts_table(results.concepts), "concepts.csv")
    csv(results.cohort.attrition, "attrition.csv")
    results.cohort.attrition.to_csv(cfg.output_dir / "attrition.csv", index=False)
    written.append(cfg.output_dir / "attrition.csv")
    csv(table1(results, cfg), "table1.csv")
    csv(cohort_export(results.data), "cohort.csv")
    csv(results.balance, "balance_full.csv")
    csv(results.balance_summary, "balance_summary.csv")
    csv(results.effects, "results.csv")
    csv(results.caliper_sensitivity, "sensitivity_caliper.csv")
    csv(results.truncation_sensitivity, "sensitivity_truncation.csv")
    csv(results.balance_tuning, "sensitivity_ps_balance_tuned.csv")
    csv(results.ps_model.coefficients.rename_axis("covariate").reset_index(), "ps_coefficients.csv")
    if not results.negative_controls.empty:
        csv(results.negative_controls, "negative_controls.csv")
    if results.evalue is not None:
        csv(pd.DataFrame([results.evalue.as_dict()]), "evalue.csv")

    figures: dict[str, Callable[[Path], None]] = {
        "attrition.png": lambda p: plot_attrition(results.cohort.attrition, cfg, p),
        "preference_score.png": lambda p: plot_preference(results, cfg, p),
        "love_plot.png": lambda p: plot_love(results.balance, cfg.smd_threshold, p),
        "km_itt.png": lambda p: plot_km(results.km, cfg, p),
        "hazard_ratios.png": lambda p: plot_effects(results.effects, cfg, p),
    }
    if not results.negative_controls.empty:
        figures["negative_controls.png"] = lambda p: plot_negative_controls(
            results.negative_controls, p
        )
    for name, fn in figures.items():
        path = fig_dir / name
        fn(path)
        written.append(path)

    summary = _summary(results, cfg)
    spath = rep / "summary.json"
    spath.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), "utf-8")
    written.append(spath)
    mpath = rep / "report.md"
    mpath.write_text(_markdown(results, cfg, summary), encoding="utf-8")
    written.append(mpath)
    for p in written:
        logger.info("Écrit : %s", p)
    return written


def _summary(results: StudyResults, cfg: StudyConfig) -> dict[str, Any]:
    final = results.cohort.attrition.iloc[-1]
    return {
        "study": cfg.name,
        "disclaimer": "Données simulées (Eunomia GiBleed) : aucune portée clinique.",
        "cohort": {"n_target": int(final["n_target"]), "n_comparator": int(final["n_comparator"])},
        "n_covariates": int(results.covariates.X.shape[1]),
        "propensity": ps_diagnostics(results),
        "n_matched_pairs": len(results.pairs),
        "balance": results.balance_summary.to_dict(orient="records"),
        "effects": results.effects.to_dict(orient="records"),
        "evalue": None if results.evalue is None else results.evalue.as_dict(),
        "notes": results.notes,
    }


def _markdown(results: StudyResults, cfg: StudyConfig, summary: dict[str, Any]) -> str:
    eff = results.effects[
        [
            "label",
            "follow_up",
            "status",
            "n_target",
            "n_comparator",
            "events_target",
            "events_comparator",
            "hr",
            "ci_low",
            "ci_high",
            "p_value",
            "ph_test_p",
            "primary",
        ]
    ]
    ps = summary["propensity"]
    parts = [
        f"# Rapport — {cfg.name}",
        "> Données **simulées** (Eunomia GiBleed). Exercice de méthode, aucune portée clinique.",
        "## Concepts résolus",
        _md_table(concepts_table(results.concepts).drop(columns=["descendant_ids"])),
        "## Attrition",
        _md_table(results.cohort.attrition),
        "## Score de propension",
        f"C retenu par validation croisée : {ps['best_c']:g} ; AUC : {ps['auc']:.3f} ; "
        f"part en équipoise : {ps['equipoise_fraction']:.1%} ; "
        f"paires appariées : {len(results.pairs)}.",
        "## Équilibre",
        _md_table(results.balance_summary),
        "## Effets (Cox, IC 95 % robustes)",
        _md_table(eff),
        "## E-value (analyse principale)",
        "Non calculée."
        if results.evalue is None
        else _md_table(pd.DataFrame([results.evalue.as_dict()])),
        "## Sensibilité post-hoc — C du PS choisi sur l'équilibre (IPTW)",
        "Ajoutée après lecture des HR, en réponse au déséquilibre résiduel de l'IPTW ; "
        "sélection sur l'équilibre seul (sans le critère). L'analyse principale est inchangée.",
        _md_table(results.balance_tuning),
        "## Sensibilité — caliper",
        _md_table(
            results.caliper_sensitivity[
                ["caliper_sd", "n_pairs", "max_abs_smd", "status", "hr", "ci_low", "ci_high"]
            ]
        ),
        "## Sensibilité — troncature IPTW",
        _md_table(
            results.truncation_sensitivity[
                [
                    "truncation",
                    "max_weight",
                    "ess",
                    "max_abs_smd",
                    "status",
                    "hr",
                    "ci_low",
                    "ci_high",
                ]
            ]
        ),
        "## Contrôles négatifs",
        "Non estimés (liste en attente de validation)."
        if results.negative_controls.empty
        else _md_table(
            results.negative_controls[
                [
                    "label",
                    "method",
                    "n_excluded_prior",
                    "status",
                    "events_target",
                    "events_comparator",
                    "hr",
                    "ci_low",
                    "ci_high",
                ]
            ]
        ),
        "## Notes",
        "\n".join(f"- {n}" for n in results.notes) or "Aucune.",
    ]
    return "\n\n".join(parts) + "\n"
