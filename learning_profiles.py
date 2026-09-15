#!/usr/bin/env python3
"""Pure schema helpers for saved-roast-backed taught profiles."""

from __future__ import annotations
from copy import deepcopy
import math
import unicodedata
from typing import Any, Iterable
SCHEMA_VERSION = 1
AUTOMATIC_PHYSICAL_DROP_SOURCE = "automatic physical dump response"
COLOR_STATES = {"hit", "miss", "unconfirmed"}
FUEL_PLAN = (
    {"event": "CHARGE", "band": "high", "intended_setting": 8},
    {"event": "TP", "band": "mid", "intended_setting": 6},
)
def _clean_text(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def normalize_identity_text(value: object) -> str:
    return _clean_text(value).casefold()


def _integer_batch(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return int(number) if math.isfinite(number) and number.is_integer() and number > 0 else None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def normalized_identity(meta: object) -> dict[str, Any]:
    source = meta if isinstance(meta, dict) else {}
    return {
        "bean": normalize_identity_text(source.get("bean")),
        "origin": normalize_identity_text(source.get("origin")),
        "batch_g": _integer_batch(source.get("batch_g")),
        "roast_level_label": normalize_identity_text(source.get("roast_level_label")),
    }


def identity_key(meta: object) -> tuple[str, str, int, str] | None:
    identity = normalized_identity(meta)
    values = (
        identity["bean"], identity["origin"], identity["batch_g"],
        identity["roast_level_label"],
    )
    return values if all(value not in (None, "") for value in values) else None


def learned_target(desired_drop_f: object, observed_drop_f: object) -> float | None:
    desired = _finite_number(desired_drop_f)
    observed = _finite_number(observed_drop_f)
    if desired is None or observed is None:
        return None
    return desired if abs(observed - desired) <= 1.0 else observed


def _physical_drop(events: object) -> dict[str, Any] | None:
    if not isinstance(events, list):
        return None
    return next(
        (
            event for event in reversed(events)
            if isinstance(event, dict) and event.get("name") == "DROP"
            and event.get("source") == AUTOMATIC_PHYSICAL_DROP_SOURCE
        ),
        None,
    )


def build_learning_block(source_roast_id: object, payload: object) -> dict[str, Any]:
    roast = payload if isinstance(payload, dict) else {}
    meta = roast.get("meta") if isinstance(roast.get("meta"), dict) else {}
    profile = roast.get("profile") if isinstance(roast.get("profile"), dict) else {}
    drop = _physical_drop(roast.get("events"))
    desired = _finite_number(meta.get("desired_drop_f"))
    if desired is None:
        desired = _finite_number(profile.get("drop_target_f"))
    observed = _finite_number(drop.get("bt")) if drop else None
    roast_id = _clean_text(source_roast_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_id": f"taught-{roast_id}",
        "source_roast_id": roast_id,
        "identity": normalized_identity(meta),
        "target_evidence": {
            "desired_drop_f": desired,
            "observed_drop_f": observed,
            "learned_drop_f": None,
        },
        "drop_evidence": {
            "name": "DROP" if drop else None,
            "source": drop.get("source") if drop else None,
        },
        "color_confirmation": {"state": "unconfirmed", "notes": ""},
        "fuel_plan": [dict(cue) for cue in FUEL_PLAN],
    }


def is_eligible(learning: object) -> bool:
    if not isinstance(learning, dict) or learning.get("schema_version") != SCHEMA_VERSION:
        return False
    source_id = learning.get("source_roast_id")
    evidence = learning.get("drop_evidence")
    return bool(
        isinstance(source_id, str) and source_id
        and learning.get("profile_id") == f"taught-{source_id}"
        and identity_key(learning.get("identity")) is not None
        and isinstance(evidence, dict)
        and evidence.get("name") == "DROP"
        and evidence.get("source") == AUTOMATIC_PHYSICAL_DROP_SOURCE
    )


def _color_state(learning: dict[str, Any]) -> str:
    confirmation = learning.get("color_confirmation")
    return confirmation.get("state", "unconfirmed") if isinstance(confirmation, dict) else "unconfirmed"


def learning_status(learning: object) -> str:
    if not is_eligible(learning):
        return "ineligible"
    state = _color_state(learning)
    if state not in COLOR_STATES:
        return "ineligible"
    if state == "miss":
        return "rejected"
    if state != "hit":
        return "pending"
    evidence = learning.get("target_evidence")
    learned = evidence.get("learned_drop_f") if isinstance(evidence, dict) else None
    return "reusable" if _finite_number(learned) is not None else "ineligible"


def with_color_confirmation(learning: object, state: object, notes: object = "") -> dict[str, Any]:
    normalized_state = normalize_identity_text(state)
    if normalized_state == "skip":
        normalized_state = "unconfirmed"
    if normalized_state not in COLOR_STATES:
        raise ValueError("Color state must be hit, miss, or unconfirmed")
    updated = deepcopy(learning) if isinstance(learning, dict) else {}
    updated.pop("status", None)
    updated["color_confirmation"] = {"state": normalized_state, "notes": _clean_text(notes)}
    evidence = updated.get("target_evidence")
    if not isinstance(evidence, dict):
        evidence = {}
        updated["target_evidence"] = evidence
    evidence["learned_drop_f"] = (
        learned_target(evidence.get("desired_drop_f"), evidence.get("observed_drop_f"))
        if normalized_state == "hit" else None
    )
    return updated


def with_observed_drop(learning: object, observed_drop_f: object) -> dict[str, Any]:
    observed = _finite_number(observed_drop_f)
    if observed is None:
        raise ValueError("Observed drop temperature must be finite")
    updated = deepcopy(learning) if isinstance(learning, dict) else {}
    updated.pop("status", None)
    evidence = updated.get("target_evidence")
    if not isinstance(evidence, dict):
        evidence = {}
        updated["target_evidence"] = evidence
    evidence["observed_drop_f"] = observed
    evidence["learned_drop_f"] = (
        learned_target(evidence.get("desired_drop_f"), observed)
        if _color_state(updated) == "hit" else None
    )
    return updated


def match_kind(profile: object, meta: object) -> str:
    if not isinstance(profile, dict) or profile.get("status", learning_status(profile)) != "reusable":
        return "none"
    stored = profile.get("identity")
    wanted = normalized_identity(meta)
    if not isinstance(stored, dict) or identity_key(stored) is None or identity_key(wanted) is None:
        return "none"
    if any(stored.get(key) != wanted[key] for key in ("bean", "origin")):
        return "none"
    return "exact" if all(
        stored.get(key) == wanted[key] for key in ("batch_g", "roast_level_label")
    ) else "suggestion"


def profile_summary(source_roast_id: object, payload: object) -> dict[str, Any] | None:
    roast = payload if isinstance(payload, dict) else {}
    learning = roast.get("learning")
    if not isinstance(learning, dict):
        return None
    source_id = _clean_text(source_roast_id)
    status = learning_status(learning)
    if learning.get("source_roast_id") != source_id:
        status = "ineligible"
    evidence = learning.get("target_evidence") if isinstance(learning.get("target_evidence"), dict) else {}
    identity = learning.get("identity") if isinstance(learning.get("identity"), dict) else {}
    return {
        "profile_id": learning.get("profile_id"),
        "source_roast_id": learning.get("source_roast_id"),
        "identity": dict(identity),
        "desired_drop_f": evidence.get("desired_drop_f"),
        "observed_drop_f": evidence.get("observed_drop_f"),
        "learned_drop_f": evidence.get("learned_drop_f"),
        "color_state": _color_state(learning),
        "status": status,
    }


def saved_roast_summaries(saved_roasts: Iterable[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    summaries = (profile_summary(roast_id, payload) for roast_id, payload in saved_roasts)
    return [summary for summary in summaries if summary is not None]
