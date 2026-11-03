from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from pmsi_demo.generate_fake_rum import (
    COLUMNS,
    FICTIF,
    INVALID_CODES,
    GeneratorConfig,
    generate_rum,
    to_rum_code,
    write_rum,
)


@pytest.fixture(scope="module")
def rum() -> pd.DataFrame:
    return pd.DataFrame(generate_rum(GeneratorConfig(n_patients=150)))


def _d(s: str) -> datetime:
    return datetime.strptime(s, "%d%m%Y")


def test_rum_code_format() -> None:
    assert to_rum_code("K92.2") == "K922"
    assert to_rum_code("I10") == "I10"


def test_deterministic() -> None:
    a = generate_rum(GeneratorConfig(n_patients=30))
    b = generate_rum(GeneratorConfig(n_patients=30))
    c = generate_rum(GeneratorConfig(n_patients=30, seed=1))
    assert a == b
    assert a != c


def test_structure(rum: pd.DataFrame) -> None:
    assert tuple(rum.columns) == COLUMNS
    assert (rum["avertissement"] == FICTIF).all()
    assert set(rum["sexe"]) <= {"1", "2"}
    codes = pd.concat([rum["dp"], rum["dr"], rum["das"].str.split("|").explode()])
    assert not codes.str.contains(".", regex=False).any()
    for col in ("date_naissance", "date_entree", "date_sortie"):
        rum[col].map(_d)  # format JJMMAAAA


def test_dates_and_units(rum: pd.DataFrame) -> None:
    start, end = rum["date_entree"].map(_d), rum["date_sortie"].map(_d)
    assert (start <= end).all()
    for _, stay in rum.groupby("n_rss"):
        assert stay["n_rum"].astype(int).tolist() == list(range(1, len(stay) + 1))
        assert (stay["mode_entree"].iloc[1:] == "6").all()  # mutations internes
        assert (stay["mode_sortie"].iloc[:-1] == "6").all()


def test_session_dp_has_related_diagnosis(rum: pd.DataFrame) -> None:
    sessions = rum["dp"].isin(["Z511", "Z5101"])
    assert (rum.loc[sessions, "dr"] != "").all()
    assert (
        rum.loc[rum["dr"] != "", "dp"].isin(["Z511", "Z5101", *map(to_rum_code, INVALID_CODES)])
    ).all()


def test_no_stay_after_death(rum: pd.DataFrame) -> None:
    for _, p in rum.groupby("ipp_fictif"):
        deaths = p.index[p["mode_sortie"] == "9"]
        assert len(deaths) <= 1
        if len(deaths):
            assert deaths[0] == p.index[-1]


def test_write(tmp_path: Path) -> None:
    path = write_rum(generate_rum(GeneratorConfig(n_patients=5)), tmp_path / "x" / "rum.csv")
    assert pd.read_csv(path, dtype=str).columns.tolist() == list(COLUMNS)
