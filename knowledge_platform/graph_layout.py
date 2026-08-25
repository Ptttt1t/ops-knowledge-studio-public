from __future__ import annotations

from typing import Any


PHASE_ALIASES = {
    "PRECHECK": "PRECHECK",
    "PRECHECK_STEPS": "PRECHECK",
    "CHECK_BEFORE_CHANGE": "PRECHECK",
    "IMPLEMENTATION": "IMPLEMENTATION",
    "IMPLEMENTATION_STEPS": "IMPLEMENTATION",
    "CHANGE_IMPLEMENT": "IMPLEMENTATION",
    "VALIDATION": "VALIDATION",
    "VALIDATION_STEPS": "VALIDATION",
    "CHANGE_VERIFIED": "VALIDATION",
    "ROLLBACK": "ROLLBACK",
    "ROLLBACK_STEPS": "ROLLBACK",
    "CHANGE_ROLLBACK": "ROLLBACK",
}

# These are layout hints, not persisted knowledge semantics. The browser adds a
# bounded local force simulation around each centroid so explicit graph edges
# can still pull related nodes toward one another.
TYPE_CENTROIDS: dict[str, tuple[float, float, float]] = {
    "CASE_BUNDLE": (-320.0, 10.0, -20.0),
    "CASE_CONTEXT": (-205.0, 145.0, -115.0),
    "PRECHECK": (-70.0, 165.0, 105.0),
    "IMPLEMENTATION": (115.0, 115.0, 135.0),
    "VALIDATION": (230.0, -20.0, 55.0),
    "ROLLBACK": (105.0, -165.0, -105.0),
    "EXECUTION_OUTCOME": (-105.0, -165.0, -145.0),
    "PROCEDURE_OTHER": (30.0, 0.0, 0.0),
    "OTHER_CARD": (-25.0, 25.0, 15.0),
    "BUSINESS_OBJECT": (335.0, 145.0, -135.0),
    "SOURCE_DOCUMENT": (-335.0, -165.0, 155.0),
}


def normalize_phase(value: Any) -> str:
    normalized = str(value or "").strip().upper().replace("-", "_")
    return PHASE_ALIASES.get(normalized, "")


def procedure_phase(payload: dict[str, Any]) -> str:
    explicit = normalize_phase(payload.get("procedure_group"))
    if explicit:
        return explicit
    phases = {
        normalized
        for item in payload.get("applicable_phases") or []
        if (normalized := normalize_phase(item))
    }
    return next(iter(phases)) if len(phases) == 1 else ""


def node_cluster(
    kind: str,
    *,
    card_type: str = "",
    semantic_payload: dict[str, Any] | None = None,
) -> str:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind == "case":
        return "CASE_BUNDLE"
    if normalized_kind == "object":
        return "BUSINESS_OBJECT"
    if normalized_kind == "source":
        return "SOURCE_DOCUMENT"
    if normalized_kind != "card":
        return "OTHER_CARD"

    normalized_type = str(card_type or "").strip().upper()
    if normalized_type == "CASE_CONTEXT":
        return "CASE_CONTEXT"
    if normalized_type == "EXECUTION_OUTCOME":
        return "EXECUTION_OUTCOME"
    if normalized_type == "PROCEDURE_STEP":
        return procedure_phase(semantic_payload or {}) or "PROCEDURE_OTHER"
    return "OTHER_CARD"


def cluster_centroid(cluster: str) -> dict[str, float]:
    x, y, z = TYPE_CENTROIDS.get(cluster, TYPE_CENTROIDS["OTHER_CARD"])
    return {"x": x, "y": y, "z": z}


def cluster_distance(left: str, right: str) -> float:
    left_point = cluster_centroid(left)
    right_point = cluster_centroid(right)
    return sum(
        (left_point[axis] - right_point[axis]) ** 2
        for axis in ("x", "y", "z")
    ) ** 0.5
