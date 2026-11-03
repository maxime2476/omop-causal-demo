"""Génère une table de RUM (résumés d'unité médicale) ENTIÈREMENT FICTIVE.

Aucune donnée réelle : patients, séjours, unités et codages sont tirés au hasard (graine fixée).
La structure imite, en très simplifié, quelques champs d'un RUM du PMSI MCO :

- ``n_rss`` / ``n_rum`` : séjour (résumé de sortie standardisé) et passage dans une unité ;
- ``ipp_fictif``, ``sexe`` (1 = homme, 2 = femme), ``date_naissance`` ;
- ``date_entree`` / ``date_sortie`` du passage dans l'unité, au format PMSI ``JJMMAAAA`` ;
- ``mode_entree`` / ``provenance`` et ``mode_sortie`` / ``destination`` (codes PMSI) ;
- ``dp`` (diagnostic principal), ``dr`` (diagnostic relié), ``das`` (diagnostics associés
  séparés par ``|``), codés en CIM-10 **sans point**, comme dans le format RUM (``K922``).

Les fréquences ne reflètent aucune épidémiologie. Quelques codes inexistants en CIM-10 FR
(codes de la CIM-10-CM américaine) sont injectés volontairement pour tester l'ETL.

Usage : ``uv run python pmsi_demo/generate_fake_rum.py [--out data/pmsi/rum_fictif.csv]``
"""

from __future__ import annotations

import argparse
import csv
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

SEED = 20251103
DEFAULT_OUT = Path("data/pmsi/rum_fictif.csv")
FICTIF = "FICTIF - données générées aléatoirement, aucun patient réel"

COLUMNS = (
    "n_rss",
    "n_rum",
    "ipp_fictif",
    "sexe",
    "date_naissance",
    "unite_medicale",
    "date_entree",
    "date_sortie",
    "mode_entree",
    "provenance",
    "mode_sortie",
    "destination",
    "dp",
    "dr",
    "das",
    "avertissement",
)

# Unités médicales fictives.
UNITS = ("UM01-CARDIO", "UM02-PNEUMO", "UM03-GASTRO", "UM04-GERIA", "UM05-ORTHO", "UM06-ONCO")

# Diagnostics principaux (code CIM-10 avec point, poids relatif). Choisis pour couvrir des cas de
# mapping variés : codes OMS à 4 caractères, codes à 3 caractères, extensions françaises.
DP_POOL: tuple[tuple[str, float], ...] = (
    ("K92.2", 4),
    ("K25.4", 2),
    ("I21.4", 3),
    ("I50.0", 4),
    ("I48.1", 2),
    ("J18.9", 5),
    ("J44.1", 3),
    ("N39.0", 4),
    ("N10", 2),
    ("S72.00", 3),
    ("S72.10", 2),
    ("I63.9", 3),
    ("A41.9", 2),
    ("K35.8", 2),
    ("R55", 2),
    ("U07.10", 1),
    ("I26.9", 2),
    ("J96.00", 1),
    ("Z51.1", 4),  # séance de chimiothérapie : impose un diagnostic relié
    ("Z51.01", 2),  # séance de radiothérapie (extension française)
)
DR_FOR_SESSION = ("C34.9", "C50.9", "C18.9")

DAS_POOL: tuple[tuple[str, float], ...] = (
    ("I10", 8),
    ("E11.9", 4),
    ("E11.90", 2),  # extension française (diabète insulinotraité)
    ("E66.00", 2),  # extension française (obésité, IMC 30-40)
    ("E78.0", 3),
    ("N18.5", 1),
    ("Z95.0", 1),
    ("F17.2", 2),
    ("D64.9", 3),
    ("E87.1", 2),
    ("E86", 2),
    ("Z74.00", 1),  # extension française (alitement)
    ("Z60.2", 2),
    ("G30.9", 1),
    ("F03", 2),
    ("Z92.1", 2),
    ("R26.8", 1),
    ("E44.0", 1),
    ("L89.1", 1),
    ("Z99.2", 1),
    ("K21.9", 2),
    ("I11.0", 1),
    ("M17.9", 1),
    ("Z96.6", 1),
    ("T81.4", 1),
)

# Codes inexistants en CIM-10 FR : forme CIM-10-CM (américaine), erreur de saisie plausible.
INVALID_CODES = ("K80.20", "K92.20", "Z96.60")
INVALID_RATE = 0.02


@dataclass(frozen=True)
class GeneratorConfig:
    """Paramètres du tirage."""

    n_patients: int = 400
    year: int = 2024
    seed: int = SEED
    invalid_rate: float = INVALID_RATE


def to_rum_code(code: str) -> str:
    """Écrit un code CIM-10 au format RUM : sans point (``K92.2`` -> ``K922``)."""
    return code.replace(".", "")


def rum_date(d: date) -> str:
    """Date au format PMSI ``JJMMAAAA``."""
    return d.strftime("%d%m%Y")


def _choice(rng: np.random.Generator, pool: tuple[tuple[str, float], ...]) -> str:
    codes = [c for c, _ in pool]
    weights = np.array([w for _, w in pool], dtype=float)
    return str(rng.choice(codes, p=weights / weights.sum()))


def _maybe_invalid(rng: np.random.Generator, code: str, rate: float) -> str:
    return str(rng.choice(INVALID_CODES)) if rng.random() < rate else code


def _das(rng: np.random.Generator, dp: str, rate: float) -> list[str]:
    n = int(rng.poisson(1.8))
    codes = [c for c, _ in DAS_POOL if c != dp]
    weights = np.array([w for c, w in DAS_POOL if c != dp], dtype=float)
    n = min(n, len(codes))
    if n == 0:
        return []
    picked = rng.choice(codes, size=n, replace=False, p=weights / weights.sum())
    return [_maybe_invalid(rng, str(c), rate) for c in picked]


def _entry(rng: np.random.Generator) -> tuple[str, str]:
    """Mode d'entrée et provenance du premier RUM d'un séjour."""
    r = rng.random()
    if r < 0.55:
        return "8", "5"  # domicile, passage par les urgences
    if r < 0.85:
        return "8", ""  # domicile, entrée directe
    return "7", "1"  # transfert depuis un autre établissement MCO


def _exit(rng: np.random.Generator) -> tuple[str, str]:
    """Mode de sortie et destination du dernier RUM d'un séjour."""
    r = rng.random()
    if r < 0.80:
        return "8", ""  # retour à domicile
    if r < 0.88:
        return "8", "7"  # structure médico-sociale (ex. EHPAD)
    if r < 0.96:
        return "7", "2"  # transfert vers un SSR
    return "9", ""  # décès


def generate_rum(cfg: GeneratorConfig) -> list[dict[str, str]]:
    """Tire la table de RUM ; une ligne par passage dans une unité médicale."""
    rng = np.random.default_rng(cfg.seed)
    rows: list[dict[str, str]] = []
    n_rss = 0
    start_of_year = date(cfg.year, 1, 1)
    for p in range(1, cfg.n_patients + 1):
        ipp = f"IPPF{p:06d}"
        sexe = str(rng.choice(["1", "2"]))
        birth = date(int(rng.integers(1930, 2005)), int(rng.integers(1, 13)), 1) + timedelta(
            days=int(rng.integers(0, 28))
        )
        day = start_of_year + timedelta(days=int(rng.integers(0, 300)))
        dead = False
        for _ in range(1 + int(rng.poisson(0.6))):
            if dead:
                break
            n_rss += 1
            n_units = int(rng.choice([1, 2, 3], p=[0.7, 0.22, 0.08]))
            units = rng.choice(UNITS, size=n_units, replace=False)
            for k, um in enumerate(units):
                los = int(rng.geometric(0.25)) - 1
                start, end = day, day + timedelta(days=los)
                mode_e, prov = _entry(rng) if k == 0 else ("6", "1")
                mode_s, dest = _exit(rng) if k == n_units - 1 else ("6", "1")
                dp = _choice(rng, DP_POOL)
                dr = str(rng.choice(DR_FOR_SESSION)) if dp in ("Z51.1", "Z51.01") else ""
                dp_out = _maybe_invalid(rng, dp, cfg.invalid_rate)
                das = _das(rng, dp, cfg.invalid_rate)
                rows.append(
                    {
                        "n_rss": f"RSSF{n_rss:07d}",
                        "n_rum": str(k + 1),
                        "ipp_fictif": ipp,
                        "sexe": sexe,
                        "date_naissance": rum_date(birth),
                        "unite_medicale": str(um),
                        "date_entree": rum_date(start),
                        "date_sortie": rum_date(end),
                        "mode_entree": mode_e,
                        "provenance": prov,
                        "mode_sortie": mode_s,
                        "destination": dest,
                        "dp": to_rum_code(dp_out),
                        "dr": to_rum_code(dr),
                        "das": "|".join(to_rum_code(c) for c in das),
                        "avertissement": FICTIF,
                    }
                )
                day = end
                dead = mode_s == "9"
            day = day + timedelta(days=int(rng.integers(20, 120)))
    logger.info(
        "%d RUM générés (%d séjours, %d patients fictifs)", len(rows), n_rss, cfg.n_patients
    )
    return rows


def write_rum(rows: list[dict[str, str]], path: Path) -> Path:
    """Écrit la table en CSV (séparateur virgule, en-tête)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Écrit : %s", path)
    return path


def main(argv: list[str] | None = None) -> None:
    """Point d'entrée en ligne de commande."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--n-patients", type=int, default=GeneratorConfig.n_patients)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rows = generate_rum(GeneratorConfig(n_patients=args.n_patients, seed=args.seed))
    write_rum(rows, args.out)


if __name__ == "__main__":
    main()
