import json
from pathlib import Path

import pytest

from omop_causal.dqd import (
    DqdResultsError,
    failed_checks,
    load_dqd_results,
    summarize_by_category,
    write_dqd_summary,
)


def _check(
    name: str, category: str, *, failed: int = 0, na: int = 0, err: int = 0, pct: float = 0.0
) -> dict[str, object]:
    return {
        "checkName": name,
        "checkLevel": "FIELD",
        "category": category,
        "subcategory": "",
        "context": "Verification",
        "cdmTableName": "PERSON",
        "cdmFieldName": "GENDER_CONCEPT_ID",
        "numViolatedRows": int(pct * 10),
        "pctViolatedRows": pct,
        "numDenominatorRows": 10,
        "thresholdValue": 0,
        "failed": failed,
        "passed": int(not (failed or na or err)),
        "isError": err,
        "notApplicable": na,
        "checkDescription": f"desc {name}",
    }


@pytest.fixture
def dqd_json(tmp_path: Path) -> Path:
    path = tmp_path / "dqd.json"
    checks = [
        _check("a", "Conformance"),
        _check("b", "Conformance", failed=1, pct=0.2),
        _check("c", "Completeness", failed=1, pct=0.9),
        _check("d", "Plausibility", na=1),
        _check("e", "Plausibility", err=1),
    ]
    path.write_text(json.dumps({"CheckResults": checks, "Overview": {}}), encoding="utf-8")
    return path


def test_status_and_summary(dqd_json: Path) -> None:
    df = load_dqd_results(dqd_json)
    assert df["status"].tolist() == ["passed", "failed", "failed", "not_applicable", "error"]
    summary = summarize_by_category(df).set_index("category")
    assert summary.loc["Conformance", "passed"] == 1
    assert summary.loc["Conformance", "failed"] == 1
    assert summary.loc["Conformance", "pct_passed_applicable"] == 50.0
    assert summary.loc["Plausibility", "n_checks"] == 2


def test_failed_checks_sorted(dqd_json: Path) -> None:
    failed = failed_checks(load_dqd_results(dqd_json))
    assert failed["checkName"].tolist() == ["c", "b"]  # Completeness < Conformance


def test_write_summary(dqd_json: Path, tmp_path: Path) -> None:
    written = write_dqd_summary(dqd_json, tmp_path / "out")
    assert {p.name for p in written} == {
        "dqd_summary_by_category.csv",
        "dqd_failed_checks.csv",
        "dqd_errors.csv",
    }


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DqdResultsError):
        load_dqd_results(tmp_path / "absent.json")
