"""Offline, rules-only abstention mapper for the trial decision standard.

No source observation can issue a decision in this milestone: source authenticity,
context, dimensions, period, domain, and purpose eligibility are not verified here.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable

_STANDARD_PATH = Path(__file__).with_name("trial_standard.json")


@dataclass(frozen=True)
class SourceObservation:
    """Untrusted observation metadata; it is not source-authentic evidence."""

    ref: str
    entity: str
    period: str
    unit: str
    source_sha256: str
    fact: str
    value: str


def load_standard() -> dict:
    """Load and minimally validate the checked-in versioned standard."""
    data = json.loads(_STANDARD_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != "airline-valuation-input-decision-standard/1":
        raise ValueError("unsupported trial standard schema")
    if not data.get("standard_version") or not isinstance(data.get("entries"), list):
        raise ValueError("trial standard must be versioned and enumerate entries")
    refs = [entry.get("ref") for entry in data["entries"]]
    if len(refs) != len(set(refs)):
        raise ValueError("trial standard contains duplicate references")
    return data


def map_observations(observations: Iterable[SourceObservation], *, entity: str) -> dict[str, dict]:
    """Return one explicit abstention per ref; never adopt observation values.

    A syntactically valid digest is not proof that source bytes, facts, locators,
    reporting contexts/dimensions, units, periods, or domains are authentic and
    compatible. Until those checks are implemented, even unique observations
    remain unverified and cannot produce positive mapped decisions.
    """
    entries = load_standard()["entries"]
    grouped: dict[str, list[SourceObservation]] = {entry["ref"]: [] for entry in entries}
    for observation in observations:
        if isinstance(observation, SourceObservation) and observation.ref in grouped:
            grouped[observation.ref].append(observation)

    result: dict[str, dict] = {}
    for entry in entries:
        ref = entry["ref"]
        observations_for_ref = grouped[ref]
        if len(observations_for_ref) > 1:
            status = "abstain_ambiguous_observations"
            reason = "multiple observations were supplied; ambiguity is unresolved and no tie-break is authorized"
        else:
            status = "abstain_source_binding_unverified"
            reason = ("no source observation is adopted; source-authentic bytes/locator, entity context and dimensions, "
                      "period, exact unit, finite value/domain, and use eligibility are not verified")
        result[ref] = {
            "status": status,
            "decision": None,
            "reason": reason,
            "uncertainty": entry["uncertainty"],
            "candidate_count": len(observations_for_ref),
            "requested_entity": entity,
        }
    return result
