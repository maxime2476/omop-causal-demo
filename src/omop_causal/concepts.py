"""Résolution des ``concept_id`` OMOP par requête (jamais codés en dur) et log."""

import logging
from dataclasses import dataclass

import duckdb
import pandas as pd

from omop_causal.config import ConceptSpec

logger = logging.getLogger(__name__)


class ConceptResolutionError(LookupError):
    """Concept introuvable ou ambigu dans ``CONCEPT``."""


@dataclass(frozen=True)
class ResolvedConcept:
    """Concept résolu et son ensemble de descendants (lui-même inclus)."""

    label: str
    concept_id: int
    concept_name: str
    vocabulary_id: str
    concept_class_id: str
    expected_concept_id: int | None
    descendant_ids: tuple[int, ...]

    @property
    def matches_expected(self) -> bool | None:
        """``True`` si l'identifiant correspond à la valeur indicative de la config."""
        if self.expected_concept_id is None:
            return None
        return self.expected_concept_id == self.concept_id


def resolve_concept(con: duckdb.DuckDBPyConnection, spec: ConceptSpec) -> ResolvedConcept:
    """Retrouve un concept standard par nom (insensible à la casse), vocabulaire et classe.

    Raises:
        ConceptResolutionError: aucun ou plusieurs concepts correspondants.
    """
    rows = con.execute(
        """
        SELECT concept_id, concept_name, vocabulary_id, concept_class_id
        FROM concept
        WHERE lower(concept_name) = lower($name)
          AND vocabulary_id = $vocab
          AND concept_class_id = $cls
          AND standard_concept = 'S'
          AND invalid_reason IS NULL
        """,
        {"name": spec.concept_name, "vocab": spec.vocabulary_id, "cls": spec.concept_class_id},
    ).fetchall()
    if len(rows) != 1:
        raise ConceptResolutionError(
            f"{spec.label} : {len(rows)} concept(s) standard trouvés pour "
            f"'{spec.concept_name}' ({spec.vocabulary_id}/{spec.concept_class_id})"
        )
    concept_id, name, vocab, cls = rows[0]
    descendants = con.execute(
        "SELECT DISTINCT descendant_concept_id FROM concept_ancestor "
        "WHERE ancestor_concept_id = $id ORDER BY 1",
        {"id": concept_id},
    ).fetchall()
    desc_ids = tuple(sorted({int(concept_id), *(int(d[0]) for d in descendants)}))
    resolved = ResolvedConcept(
        label=spec.label,
        concept_id=int(concept_id),
        concept_name=str(name),
        vocabulary_id=str(vocab),
        concept_class_id=str(cls),
        expected_concept_id=spec.expected_concept_id,
        descendant_ids=desc_ids,
    )
    logger.info(
        "Concept %s -> %d '%s' (%s/%s), %d descendant(s) incl. lui-même",
        spec.label,
        resolved.concept_id,
        resolved.concept_name,
        vocab,
        cls,
        len(desc_ids),
    )
    if resolved.matches_expected is False:
        logger.warning(
            "Concept %s : identifiant résolu %d différent de la valeur indicative %s",
            spec.label,
            resolved.concept_id,
            spec.expected_concept_id,
        )
    return resolved


def concepts_table(concepts: list[ResolvedConcept]) -> pd.DataFrame:
    """Table de traçabilité des concepts résolus (écrite dans le rapport)."""
    return pd.DataFrame(
        [
            {
                "label": c.label,
                "concept_id": c.concept_id,
                "concept_name": c.concept_name,
                "vocabulary_id": c.vocabulary_id,
                "concept_class_id": c.concept_class_id,
                "expected_concept_id": c.expected_concept_id,
                "matches_expected": c.matches_expected,
                "n_descendants": len(c.descendant_ids),
                "descendant_ids": " ".join(str(d) for d in c.descendant_ids),
            }
            for c in concepts
        ]
    )
