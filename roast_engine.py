#!/usr/bin/env python3
"""Adaptive, machine-local roast analysis for the IR-5 companion."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from math import isfinite
from typing import Any


EVENT_ORDER = ("CHARGE", "TP", "DRY_END", "FCs", "FCe", "SCs", "SCe", "DROP")
PHYSICAL_DROP_MIN_SECONDS = 90.0
PHYSICAL_DROP_WINDOW_SAMPLES = 3
PHYSICAL_DROP_MIN_RISE_F = 8.0
PHYSICAL_DROP_MIN_ROR = 45.0
PHYSICAL_DROP_MIN_ACCELERATION = 25.0
PHYSICAL_DROP_MIN_ET_STEP_F = 2.0
PHYSICAL_DROP_MIN_ET_FALL_F = 8.0
PHYSICAL_DROP_CANDIDATE_MAX_AGE_SECONDS = 30.0
PHYSICAL_DROP_CONFIRM_ET_FALL_F = 20.0
PHYSICAL_DROP_CONFIRM_MAX_ET_BT_GAP_F = 15.0
AUTOMATIC_PHYSICAL_DROP_SOURCE = "automatic physical dump response"
IR5_DOCUMENTED_MIN_BATCH_G = 453


@dataclass(frozen=True)
class RoastProfile:
    key: str
    label: str
    fc_start_f: float
    second_crack_f: float
    drop_target_f: float
    drop_ceiling_f: float
    development_seconds: int
    fc_end_seconds: int
    min_fc_seconds: int
    description: str
    mode: str = "normal"
    charge_et_low_f: float | None = None
    charge_et_high_f: float | None = None
    target_ror_low: float | None = None
    target_ror_high: float | None = None
    sample_start_seconds: int | None = None
    dump_window_seconds: int | None = None
    max_seconds: int | None = None
    absolute_ceiling_f: float | None = None
    manual_dump: bool = False


PROFILES = {
    "light": RoastProfile(
        key="light",
        label="Light",
        fc_start_f=362.0,
        second_crack_f=405.0,
        drop_target_f=390.0,
        drop_ceiling_f=392.0,
        development_seconds=75,
        fc_end_seconds=38,
        min_fc_seconds=330,
        description="IR-5 light v2: aim near 390°F / 1:15; 392°F is a soft ceiling to protect the light endpoint.",
    ),
    "medium": RoastProfile(
        key="medium",
        label="Medium",
        fc_start_f=362.0,
        second_crack_f=405.0,
        drop_target_f=400.0,
        drop_ceiling_f=405.0,
        development_seconds=95,
        fc_end_seconds=45,
        min_fc_seconds=330,
        description="Starter profile: longer development and a higher drop target.",
    ),
    "correction": RoastProfile(
        key="correction",
        label="Correction",
        fc_start_f=370.0,
        second_crack_f=380.0,
        drop_target_f=370.0,
        drop_ceiling_f=375.0,
        development_seconds=300,
        fc_end_seconds=0,
        min_fc_seconds=0,
        description="Experimental re-roast: sample from 3:00, do not wait for FC, and dump manually by 5:00.",
        mode="correction",
        charge_et_low_f=285.0,
        charge_et_high_f=300.0,
        target_ror_low=8.0,
        target_ror_high=12.0,
        sample_start_seconds=180,
        dump_window_seconds=210,
        max_seconds=300,
        absolute_ceiling_f=380.0,
        manual_dump=True,
    ),
}


def profile_payloads() -> dict[str, dict[str, Any]]:
    return {key: asdict(value) for key, value in PROFILES.items()}


def effective_profile(
    profile: str, batch_g: int | None = None, desired_drop_f: float | None = None
) -> RoastProfile:
    selected = PROFILES.get(profile, PROFILES["light"])
    if selected.key == "light" and batch_g == 300:
        selected = replace(
            selected,
            drop_target_f=403.0,
            drop_ceiling_f=405.0,
            description="Experimental 300 g Warmikuna calibration: target 403°F with a 405°F ceiling.",
        )
    if desired_drop_f is not None:
        if selected.mode == "correction":
            raise ValueError("Correction roasts use a fixed drop target")
        if isinstance(desired_drop_f, bool) or not isinstance(desired_drop_f, (int, float)):
            raise ValueError("Desired drop temperature must be a number")
        desired_drop_f = float(desired_drop_f)
        if not isfinite(desired_drop_f) or not 250 <= desired_drop_f <= 500:
            raise ValueError("Desired drop temperature must be a finite value from 250°F to 500°F")
        selected = replace(
            selected,
            drop_target_f=desired_drop_f,
            drop_ceiling_f=max(selected.drop_ceiling_f, desired_drop_f),
        )
    return selected


def batch_guidance(batch_g: int | None, level: str = "light") -> dict[str, Any]:
    underload = batch_g is not None and batch_g < IR5_DOCUMENTED_MIN_BATCH_G
    calibrated = level == "light" and batch_g == 300
    if calibrated:
        title = "Experimental 300 g Warmikuna calibration"
        message = (
            "300 g is below the documented IR-5 minimum of 453 g. The light profile uses the experimental "
            "403°F target and 405°F ceiling; automatic times remain unchanged."
        )
    elif underload:
        title = f"Experimental {batch_g} g IR-5 batch"
        message = (
            f"{batch_g} g is below the documented IR-5 minimum of {IR5_DOCUMENTED_MIN_BATCH_G} g. "
            "Automatic times and temperature limits are unchanged. Use the curve, RoR, crack, color, and aroma, "
            "with manual fallback available."
        )
    else:
        title = "IR-5 batch guidance"
        message = "Automatic times and temperature limits are unchanged."
    return {
        "batch_g": batch_g,
        "mode": "experimental_underload" if underload else ("unset" if batch_g is None else "documented_range"),
        "supported_min_g": IR5_DOCUMENTED_MIN_BATCH_G,
        "time_factor": 1.0,
        "time_adjustment_applied": False,
        "temperature_adjustment_applied": calibrated,
        "title": title,
        "message": message,
    }


def _linear_slope(points: list[tuple[float, float]], seconds: float = 18.0) -> float | None:
    if len(points) < 3:
        return None
    latest = points[-1][0]
    window = [(x, y) for x, y in points if x >= latest - seconds]
    if len(window) < 3:
        return None
    mean_x = sum(x for x, _ in window) / len(window)
    mean_y = sum(y for _, y in window) / len(window)
    denominator = sum((x - mean_x) ** 2 for x, _ in window)
    if denominator == 0:
        return None
    per_second = sum((x - mean_x) * (y - mean_y) for x, y in window) / denominator
    return per_second * 60.0


class RoastAnalyzer:
    def __init__(
        self, profile: str = "light", batch_g: int | None = None, desired_drop_f: float | None = None
    ) -> None:
        self.profile = effective_profile(profile, batch_g, desired_drop_f)
        self.points: list[dict[str, float]] = []
        self.events: dict[str, dict[str, Any]] = {}
        self.alerts: list[dict[str, Any]] = []
        self.ror = 0.0
        self._alerted: set[str] = set()
        self._physical_drop_candidate: dict[str, Any] | None = None

    def set_profile(
        self, profile: str, batch_g: int | None = None, desired_drop_f: float | None = None
    ) -> None:
        self.profile = effective_profile(profile, batch_g, desired_drop_f)
        self._physical_drop_candidate = None

    def mark_event(
        self,
        name: str,
        *,
        elapsed: float,
        et: float,
        bt: float,
        confidence: float = 1.0,
        source: str = "manual",
        replace: bool = True,
    ) -> dict[str, Any]:
        if name in self.events and not replace:
            return self.events[name]
        event = {
            "name": name,
            "elapsed": round(elapsed, 2),
            "et": round(et, 2),
            "bt": round(bt, 2),
            "confidence": round(confidence, 2),
            "source": source,
        }
        self.events[name] = event
        if name == "DROP":
            self._physical_drop_candidate = None
        return event

    def _add_alert(self, kind: str, message: str, elapsed: float, sound: str) -> None:
        token = f"{kind}:{round(elapsed)}"
        if token in self._alerted:
            return
        self._alerted.add(token)
        self.alerts.append(
            {
                "id": len(self.alerts) + 1,
                "kind": kind,
                "message": message,
                "elapsed": round(elapsed, 2),
                "sound": sound,
            }
        )

    def add_point(self, elapsed: float, et: float, bt: float, *, auto: bool = True) -> None:
        point = {"elapsed": float(elapsed), "et": float(et), "bt": float(bt)}
        self.points.append(point)
        bt_points = [(p["elapsed"], p["bt"]) for p in self.points]
        self.ror = _linear_slope(bt_points) or 0.0
        if auto:
            self._detect_live(point)

    def _detect_live(self, point: dict[str, float]) -> None:
        elapsed, et, bt = point["elapsed"], point["et"], point["bt"]
        recent = self.points[-20:]

        if "CHARGE" not in self.events and len(recent) >= 5:
            recent_high = max(p["bt"] for p in recent)
            if recent_high - bt >= 12 and self.ror < -25:
                idx = max(0, len(self.points) - len(recent))
                for offset in range(1, len(recent)):
                    if recent[offset - 1]["bt"] - recent[offset]["bt"] >= 2:
                        idx += offset - 1
                        break
                p = self.points[idx]
                event = self.mark_event(
                    "CHARGE", elapsed=p["elapsed"], et=p["et"], bt=p["bt"], confidence=0.82, source="adaptive", replace=False
                )
                self._add_alert("stage", "Charge detected", event["elapsed"], "stage")

        charge = self.events.get("CHARGE")
        roast_elapsed = elapsed - charge["elapsed"] if charge else elapsed

        if not charge:
            self._physical_drop_candidate = None
        elif "DROP" not in self.events and len(self.points) > PHYSICAL_DROP_WINDOW_SAMPLES:
            onset = self.points[-(PHYSICAL_DROP_WINDOW_SAMPLES + 1)]
            response = self.points[-(PHYSICAL_DROP_WINDOW_SAMPLES + 1):]
            earlier_points = [(p["elapsed"], p["bt"]) for p in self.points[: -(PHYSICAL_DROP_WINDOW_SAMPLES + 1)]]
            earlier_ror = _linear_slope(earlier_points, seconds=14)
            bt_spike = (
                bt - onset["bt"] >= PHYSICAL_DROP_MIN_RISE_F
                and self.ror > PHYSICAL_DROP_MIN_ROR
                and earlier_ror is not None
                and self.ror - earlier_ror > PHYSICAL_DROP_MIN_ACCELERATION
            )
            et_deltas = [current["et"] - previous["et"] for previous, current in zip(response, response[1:])]
            bt_deltas = [current["bt"] - previous["bt"] for previous, current in zip(response, response[1:])]
            et_collapse = (
                all(delta <= -PHYSICAL_DROP_MIN_ET_STEP_F for delta in et_deltas)
                and onset["et"] - et >= PHYSICAL_DROP_MIN_ET_FALL_F
                and all(delta <= 0 for delta in bt_deltas)
            )
            eligible_onset = onset["elapsed"] - charge["elapsed"] >= PHYSICAL_DROP_MIN_SECONDS
            if eligible_onset and bt_spike:
                event = self.mark_event(
                    "DROP", **onset, confidence=0.9, source=AUTOMATIC_PHYSICAL_DROP_SOURCE, replace=False
                )
                self._add_alert("stage", "Physical dump detected from probe response", event["elapsed"], "stage")
            candidate_expired = False
            candidate = self._physical_drop_candidate
            if "DROP" not in self.events and candidate:
                candidate_age = elapsed - candidate["onset"]["elapsed"]
                if candidate_age > PHYSICAL_DROP_CANDIDATE_MAX_AGE_SECONDS:
                    self._physical_drop_candidate = None
                    candidate_expired = True
                elif elapsed > candidate["detected_elapsed"]:
                    candidate_points = self.points[candidate["point_index"]:]
                    if (
                        candidate["onset"]["et"] - et >= PHYSICAL_DROP_CONFIRM_ET_FALL_F
                        and bt - min(p["bt"] for p in candidate_points) >= PHYSICAL_DROP_MIN_RISE_F
                        and et - bt <= PHYSICAL_DROP_CONFIRM_MAX_ET_BT_GAP_F
                        and self.ror > PHYSICAL_DROP_MIN_ROR
                    ):
                        event = self.mark_event(
                            "DROP", **candidate["onset"], confidence=0.9,
                            source=AUTOMATIC_PHYSICAL_DROP_SOURCE, replace=False,
                        )
                        self._add_alert(
                            "stage", "Physical dump detected from probe response", event["elapsed"], "stage"
                        )
            if (
                "DROP" not in self.events
                and self._physical_drop_candidate is None
                and not candidate_expired
                and eligible_onset
                and et_collapse
            ):
                self._physical_drop_candidate = {
                    "onset": onset.copy(),
                    "detected_elapsed": elapsed,
                    "point_index": len(self.points) - PHYSICAL_DROP_WINDOW_SAMPLES - 1,
                }

        if charge and "TP" not in self.events and roast_elapsed > 35 and self.ror > 4:
            candidates = [p for p in self.points if p["elapsed"] >= charge["elapsed"]]
            minimum = min(candidates, key=lambda p: p["bt"])
            event = self.mark_event("TP", **minimum, confidence=0.95, source="adaptive", replace=False)
            self._add_alert("stage", "Turning point", event["elapsed"], "stage")

        if self.profile.mode == "correction":
            correction_alerts = (
                (roast_elapsed >= self.profile.sample_start_seconds, "correction_sample", "stage", "CORRECTION: sample now, then every 20–30 seconds", "stage"),
                (roast_elapsed >= self.profile.max_seconds - 30, "correction_prepare", "warning", "CORRECTION: prepare Cooling Bin, agitator ON, and pilot only", "warning"),
                (roast_elapsed >= self.profile.max_seconds - 5, "correction_five_seconds", "dump_warning", "CORRECTION: MANUAL DUMP IN 5 SECONDS", "dump_warning"),
                (bt >= self.profile.drop_ceiling_f, "correction_preferred_ceiling", "ceiling_warning", "CORRECTION: preferred 375°F ceiling reached — prepare to dump", "warning"),
                (bt >= self.profile.absolute_ceiling_f, "correction_hard_ceiling", "dump_warning", "CORRECTION: 380°F HARD LIMIT — MANUAL DUMP NOW", "dump_warning"),
            )
            for active, token, kind, message, sound in correction_alerts:
                if active and token not in self._alerted:
                    self._alerted.add(token)
                    self._add_alert(kind, message, elapsed, sound)
            return

        if charge and "DRY_END" not in self.events and roast_elapsed > 150 and bt >= 300 and self.ror > 0:
            event = self.mark_event("DRY_END", elapsed=elapsed, et=et, bt=bt, confidence=0.88, source="profile+curve", replace=False)
            self._add_alert("stage", "Drying complete — Maillard", event["elapsed"], "stage")

        if (
            charge
            and "FCs" not in self.events
            and roast_elapsed >= self.profile.min_fc_seconds
            and bt >= self.profile.fc_start_f
            and self.ror > 0
        ):
            event = self.mark_event("FCs", elapsed=elapsed, et=et, bt=bt, confidence=0.58, source="curve candidate", replace=False)
            self._add_alert("stage", "First crack window opened — listen", event["elapsed"], "first_crack")

        fcs = self.events.get("FCs")
        if fcs and "FCe" not in self.events and elapsed - fcs["elapsed"] >= self.profile.fc_end_seconds:
            event = self.mark_event("FCe", elapsed=elapsed, et=et, bt=bt, confidence=0.32, source="estimated", replace=False)
            self._add_alert("stage", "First crack likely ending", event["elapsed"], "stage")

        if "DROP" in self.events:
            return

        if fcs and "SCs" not in self.events and bt >= self.profile.second_crack_f and elapsed - fcs["elapsed"] > 25:
            event = self.mark_event("SCs", elapsed=elapsed, et=et, bt=bt, confidence=0.55, source="profile+curve", replace=False)
            self._add_alert("stage", "Second crack may be starting", event["elapsed"], "warning")

        scs = self.events.get("SCs")
        if scs and "SCe" not in self.events and elapsed - scs["elapsed"] >= 35:
            event = self.mark_event("SCe", elapsed=elapsed, et=et, bt=bt, confidence=0.35, source="estimated", replace=False)
            self._add_alert("stage", "Second crack may be ending", event["elapsed"], "stage")

        prediction = self.drop_prediction()
        ceiling_near = bt >= self.profile.drop_ceiling_f - 2
        target_near = prediction is not None and prediction["seconds_to_drop"] <= 5
        if charge and "TP" in self.events and (ceiling_near or target_near) and "drop_approach" not in self._alerted:
            target_delta = self.profile.drop_target_f - bt
            ceiling_delta = self.profile.drop_ceiling_f - bt
            if target_delta > 0:
                target_distance = f"{target_delta:.1f}°F TO {self.profile.drop_target_f:.0f}°F TARGET"
                message = (
                    f"DUMP IN {prediction['seconds_to_drop']:.1f} SECONDS — {target_distance}"
                    if prediction is not None
                    else f"DROP APPROACH — {target_distance}"
                )
            elif target_delta < 0:
                message = (
                    f"DROP TARGET PASSED BY {-target_delta:.1f}°F — "
                    f"TARGET {self.profile.drop_target_f:.0f}°F"
                )
            else:
                message = f"DROP TARGET REACHED — TARGET {self.profile.drop_target_f:.0f}°F"
            if self.profile.drop_ceiling_f > self.profile.drop_target_f:
                if ceiling_delta > 0:
                    message += f" · {ceiling_delta:.1f}°F TO {self.profile.drop_ceiling_f:.0f}°F CEILING"
                elif ceiling_delta < 0:
                    message += f" · CEILING PASSED BY {-ceiling_delta:.1f}°F"
                else:
                    message += f" · {self.profile.drop_ceiling_f:.0f}°F CEILING REACHED"
            self._alerted.add("drop_approach")
            self._add_alert(
                "ceiling_warning" if ceiling_near else "dump_warning",
                message,
                elapsed,
                "dump_warning",
            )

    def drop_prediction(self) -> dict[str, float] | None:
        if self.profile.manual_dump:
            return None
        if not self.points or "DROP" in self.events:
            return None
        current = self.points[-1]
        if current["bt"] >= self.profile.drop_target_f:
            target = current["elapsed"]
        elif self.ror > 1:
            target = current["elapsed"] + (self.profile.drop_target_f - current["bt"]) / (self.ror / 60.0)
        else:
            return None
        return {
            "target_elapsed": round(target, 2),
            "seconds_to_drop": round(max(0.0, target - current["elapsed"]), 2),
            "target_temp_f": self.profile.drop_target_f,
        }

    def backfill(self) -> dict[str, dict[str, Any]]:
        """Fill curve-inferable missing events. Low-confidence crack events stay labeled."""
        if len(self.points) < 8:
            return self.events
        bt_points = [(p["elapsed"], p["bt"]) for p in self.points]

        if "CHARGE" not in self.events:
            slopes = []
            for idx in range(2, len(self.points)):
                dt = self.points[idx]["elapsed"] - self.points[idx - 2]["elapsed"]
                if dt > 0:
                    slopes.append(((self.points[idx]["bt"] - self.points[idx - 2]["bt"]) / dt, idx))
            if slopes:
                _, idx = min(slopes)
                p = self.points[max(0, idx - 2)]
                self.mark_event("CHARGE", **p, confidence=0.66, source="backfilled")

        charge = self.events.get("CHARGE")
        if charge and "TP" not in self.events:
            candidates = [p for p in self.points if charge["elapsed"] <= p["elapsed"] <= charge["elapsed"] + 180]
            if candidates:
                self.mark_event("TP", **min(candidates, key=lambda p: p["bt"]), confidence=0.94, source="backfilled")

        if self.profile.mode == "correction":
            return self.events

        if charge and "DRY_END" not in self.events:
            for p in self.points:
                if p["elapsed"] - charge["elapsed"] > 120 and p["bt"] >= 300:
                    self.mark_event("DRY_END", **p, confidence=0.82, source="backfilled")
                    break

        if charge and "FCs" not in self.events:
            for p in self.points:
                if p["elapsed"] - charge["elapsed"] >= self.profile.min_fc_seconds and p["bt"] >= self.profile.fc_start_f:
                    self.mark_event("FCs", **p, confidence=0.62, source="backfilled profile estimate")
                    break

        fcs = self.events.get("FCs")
        if fcs and "FCe" not in self.events:
            p = min(self.points, key=lambda x: abs(x["elapsed"] - (fcs["elapsed"] + self.profile.fc_end_seconds)))
            self.mark_event("FCe", **p, confidence=0.35, source="backfilled estimate")

        if fcs and "SCs" not in self.events:
            drop_elapsed = self.events.get("DROP", {}).get("elapsed", float("inf"))
            candidates = [p for p in self.points if fcs["elapsed"] + 25 < p["elapsed"] < drop_elapsed and p["bt"] >= self.profile.second_crack_f]
            if candidates:
                self.mark_event("SCs", **candidates[0], confidence=0.45, source="backfilled profile estimate")

        scs = self.events.get("SCs")
        if scs and "SCe" not in self.events:
            p = min(self.points, key=lambda x: abs(x["elapsed"] - (scs["elapsed"] + 35)))
            self.mark_event("SCe", **p, confidence=0.3, source="backfilled estimate")

        return self.events

    def payload(self) -> dict[str, Any]:
        prediction = self.drop_prediction()
        return {
            "profile": asdict(self.profile),
            "points": self.points,
            "events": [self.events[name] for name in EVENT_ORDER if name in self.events],
            "alerts": self.alerts,
            "ror": round(self.ror, 2),
            "drop_prediction": prediction,
        }
