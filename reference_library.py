#!/usr/bin/env python3
"""Persistent offline roast references and transparent FC likelihood scoring."""

from __future__ import annotations

import ast
import hashlib
import json
import math
from pathlib import Path
from typing import Any


EVENT_NAMES = ("CHARGE", "DRY_END", "FCs", "FCe", "SCs", "SCe", "DROP", "COOL")


def _sigmoid(value: float) -> float:
    return 1 / (1 + math.exp(-max(-20, min(20, value))))


def parse_artisan(content: str, filename: str, overrides: dict[str, str] | None = None) -> dict[str, Any]:
    source = ast.literal_eval(content)
    celsius = source.get("mode") == "C"
    to_f = (lambda value: float(value) * 9 / 5 + 32) if celsius else float
    points = [
        {"elapsed": round(float(t), 3), "et": to_f(et), "bt": to_f(bt)}
        for t, et, bt in zip(source["timex"], source["temp1"], source["temp2"])
    ]
    events = []
    for name, idx in zip(EVENT_NAMES, source.get("timeindex", [])):
        if idx and idx < len(points):
            confidence = 0.6 if name in ("FCs", "FCe", "SCs", "SCe") else 0.9
            events.append({"name": name, **points[idx], "source": "imported operator marker", "confidence": confidence})
    supplied = overrides or {}
    weight = source.get("weight", [0, 0, ""])
    identifier = hashlib.sha256((filename + content[:500]).encode()).hexdigest()[:12]
    return {
        "id": identifier,
        "name": supplied.get("name") or source.get("beans") or Path(filename).stem,
        "origin": supplied.get("origin") or "Unknown",
        "level": supplied.get("level") or "unknown",
        "machine": supplied.get("machine") or source.get("roastertype") or "Unknown machine",
        "batch": f"{weight[0]} {weight[2]}" if weight and weight[0] else "Unknown batch",
        "source": filename,
        "points": points,
        "events": events,
    }


class ReferenceLibrary:
    def __init__(self, root: Path, builtin_path: Path, bundled_root: Path | None = None) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.references: dict[str, dict[str, Any]] = {}
        self.active_id: str | None = None
        if builtin_path.exists():
            reference = json.loads(builtin_path.read_text())
            reference.update(
                {
                    "id": "ir5-reference-20260730",
                    "name": "Your IR-5 roast #1",
                    "origin": "Unknown",
                    "level": "light candidate",
                    "machine": "Diedrich IR-5 / same probes",
                    "source": "Local Artisan roast; FC marker unconfirmed",
                }
            )
            for event in reference.get("events", []):
                if event["name"] in ("FCs", "FCe", "SCs", "SCe"):
                    event["confidence"] = 0.55
                    event["source"] = "operator candidate"
            self.references[reference["id"]] = reference
            self.active_id = reference["id"]
        for path in sorted(self.root.glob("*.json")):
            try:
                reference = json.loads(path.read_text())
                self.references[reference["id"]] = reference
            except Exception:
                continue
        if bundled_root and bundled_root.exists():
            for path in sorted(bundled_root.glob("*.json")):
                try:
                    reference = json.loads(path.read_text())
                    self.references[reference["id"]] = reference
                except Exception:
                    continue

    def import_alog(self, content: str, filename: str, overrides: dict[str, str]) -> dict[str, Any]:
        reference = parse_artisan(content, filename, overrides)
        (self.root / f"{reference['id']}.json").write_text(json.dumps(reference, indent=2) + "\n")
        self.references[reference["id"]] = reference
        self.active_id = reference["id"]
        return self.summary(reference)

    def set_active(self, reference_id: str) -> None:
        if reference_id not in self.references:
            raise ValueError("Unknown reference")
        self.active_id = reference_id

    def activate_saved(self, reference: dict[str, Any]) -> dict[str, Any]:
        for reference_id in [key for key, value in self.references.items() if value.get("saved_roast_ids")]:
            self.references.pop(reference_id)
        self.references[reference["id"]] = reference
        self.active_id = reference["id"]
        return self.summary(reference)

    @staticmethod
    def saved_reference(sources: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
        def normalize(roast_id: str, payload: dict[str, Any]) -> dict[str, Any]:
            charge = next((event for event in payload["events"] if event["name"] == "CHARGE"), None)
            if charge is None:
                raise ValueError("Saved roast has no Charge event")
            offset = charge["elapsed"]
            points = [
                {"elapsed": round(point["elapsed"] - offset, 3), "et": point["et"], "bt": point["bt"]}
                for point in payload["points"] if point["elapsed"] >= offset
            ]
            if not points:
                raise ValueError("Saved roast has no post-Charge points")
            events = [
                {**event, "elapsed": round(event["elapsed"] - offset, 3)}
                for event in payload["events"] if event["elapsed"] >= offset
            ]
            meta = payload["meta"]
            name = " · ".join(str(meta.get(key, "")).strip() for key in ("origin", "bean") if str(meta.get(key, "")).strip())
            return {"roast_id": roast_id, "name": name or roast_id, "meta": meta, "points": points, "events": events}

        def interpolate(points: list[dict[str, float]], elapsed: int) -> tuple[float, float]:
            for left, right in zip(points, points[1:]):
                if left["elapsed"] <= elapsed <= right["elapsed"]:
                    span = right["elapsed"] - left["elapsed"]
                    ratio = 0 if span == 0 else (elapsed - left["elapsed"]) / span
                    return tuple(left[key] + (right[key] - left[key]) * ratio for key in ("et", "bt"))
            point = points[0] if elapsed <= points[0]["elapsed"] else points[-1]
            return point["et"], point["bt"]

        normalized = [normalize(*source) for source in sources]
        if len(normalized) == 1:
            source = normalized[0]
            meta = source["meta"]
            return {
                "id": f"saved-roast-{source['roast_id']}", "name": source["name"],
                "origin": meta.get("origin", ""), "level": meta.get("level", ""),
                "machine": "Peak Roasting saved roast", "batch": f"{meta.get('batch_g')} g" if meta.get("batch_g") else "Unknown batch",
                "source": f"Saved roast {source['roast_id']}", "saved_roast_ids": [source["roast_id"]],
                "points": source["points"], "events": source["events"],
            }
        first, second = normalized
        duration = math.floor(min(first["points"][-1]["elapsed"], second["points"][-1]["elapsed"]))
        points = []
        for elapsed in range(duration + 1):
            one, two = interpolate(first["points"], elapsed), interpolate(second["points"], elapsed)
            points.append({"elapsed": elapsed, "et": round((one[0] + two[0]) / 2, 3), "bt": round((one[1] + two[1]) / 2, 3)})
        first_events, second_events = ({event["name"]: event for event in source["events"]} for source in normalized)
        events = [
            {
                "name": name,
                **{key: round((first_events[name][key] + second_events[name][key]) / 2, 3) for key in ("elapsed", "et", "bt")},
                "confidence": round((first_events[name].get("confidence", 0.6) + second_events[name].get("confidence", 0.6)) / 2, 2),
                "source": "averaged saved roast markers",
            }
            for name in first_events if name in second_events
        ]
        roast_ids = [first["roast_id"], second["roast_id"]]
        return {
            "id": f"saved-roast-average-{'-'.join(roast_ids)}", "name": f"Average · {first['name']} + {second['name']}",
            "origin": " + ".join(str(value) for value in (first["meta"].get("origin"), second["meta"].get("origin")) if value),
            "level": "average", "machine": "Peak Roasting saved-roast average", "batch": "Average of two roasts",
            "source": f"Average of saved roasts {' + '.join(roast_ids)}", "saved_roast_ids": roast_ids,
            "points": points, "events": events,
        }

    @staticmethod
    def summary(reference: dict[str, Any]) -> dict[str, Any]:
        event_names = [event["name"] for event in reference.get("events", [])]
        return {key: reference.get(key) for key in ("id", "name", "origin", "level", "machine", "batch", "source")} | {"events": event_names}

    def summaries(self) -> list[dict[str, Any]]:
        return [self.summary(reference) for reference in self.references.values()]

    def active(self) -> dict[str, Any] | None:
        return self.references.get(self.active_id) if self.active_id else None

    def fc_likelihood(self, points: list[dict[str, float]], events: dict[str, dict[str, Any]]) -> dict[str, Any]:
        if not points:
            return {"probability": 0, "references_used": 0, "explanation": "Waiting for curve data."}
        current = points[-1]
        charge = events.get("CHARGE")
        if not charge:
            return {"probability": 0, "references_used": 0, "explanation": "Waiting for charge detection."}
        time_scores: list[tuple[float, float]] = []
        temp_scores: list[tuple[float, float]] = []
        for reference in self.references.values():
            mapped = {event["name"]: event for event in reference.get("events", [])}
            if "CHARGE" not in mapped or "FCs" not in mapped:
                continue
            machine = str(reference.get("machine", "")).lower()
            compatible = "diedrich" in machine and ("ir-5" in machine or "ir5" in machine)
            weight = 1.0 if compatible else 0.35
            ref_fc_time = mapped["FCs"]["elapsed"] - mapped["CHARGE"]["elapsed"]
            roast_time = current["elapsed"] - charge["elapsed"]
            time_scores.append((_sigmoid((roast_time - ref_fc_time) / 24), weight))
            if compatible:
                temp_scores.append((_sigmoid((current["bt"] - mapped["FCs"]["bt"]) / 4), weight))
        if not time_scores:
            return {"probability": 0, "references_used": 0, "explanation": "No reference has both Charge and FCs markers."}
        weighted_time = sum(value * weight for value, weight in time_scores) / sum(weight for _, weight in time_scores)
        if temp_scores:
            weighted_temp = sum(value * weight for value, weight in temp_scores) / sum(weight for _, weight in temp_scores)
            probability = 0.52 * weighted_time + 0.48 * weighted_temp
            explanation = "Time alignment plus same-machine BT calibration; imported crack markers are treated as candidates."
        else:
            probability = weighted_time * 0.72
            explanation = "Time alignment only; temperatures from unlike machines/probes were not compared directly."
        if "FCs" in events:
            probability = max(probability, events["FCs"].get("confidence", 0.5))
        return {"probability": round(probability, 3), "references_used": len(time_scores), "explanation": explanation}
