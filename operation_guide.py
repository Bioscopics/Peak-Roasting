#!/usr/bin/env python3
"""Pure operator-prompt logic. This module never actuates hardware."""

from __future__ import annotations

from typing import Any


SMALL_BATCH_G = 300


def _is_manual_observation(event: dict[str, Any] | None) -> bool:
    source = str((event or {}).get("source", "")).strip().lower().replace("_", " ").replace("-", " ")
    return source.startswith("manual")


def _hopper_copy(next_roast: dict[str, Any] | None) -> tuple[str, str, str] | None:
    """Return stable reminder copy for a normalized queued roast."""

    if not next_roast:
        return None
    queue_id = str(next_roast.get("id") or "").strip()
    if not queue_id:
        return None

    bean = str(next_roast.get("bean") or "").strip()
    origin = str(next_roast.get("origin") or "").strip()
    level = str(next_roast.get("level") or "").strip()
    batch_g = next_roast.get("batch_g")
    target_f = next_roast.get("drop_target_f")

    title_parts = ["LOAD NEXT HOPPER NOW"]
    if bean:
        title_parts.append(bean.upper())
    if isinstance(batch_g, (int, float)) and not isinstance(batch_g, bool):
        title_parts.append(f"{batch_g:g} G")

    descriptor = " · ".join(part for part in (origin, bean, level.title()) if part) or "the next roast"
    target = (
        f"Next roast drop target {target_f:g}°F."
        if isinstance(target_f, (int, float)) and not isinstance(target_f, bool)
        else "Next roast drop target is unavailable."
    )
    detail = (
        f"Stage green beans for {descriptor}. {target} Do not charge them until the current roast "
        "has dropped and the next roast is armed. Reminder only; hopper position/content is not sensed."
    )
    return f"hopper_next:{queue_id}", " · ".join(title_parts), detail


def fuel_guidance(
    *,
    recording: bool,
    bt: float | None,
    events: dict[str, dict[str, Any]],
    prediction: dict[str, float] | None,
    drop_target_f: float,
    post_drop_seconds: float | None = None,
    profile_key: str = "light",
    elapsed: float | None = None,
    batch_g: int | None = None,
) -> dict[str, Any]:
    """Return schedule-based fuel advice; no setting is sensed or applied."""

    if post_drop_seconds is not None or "DROP" in events:
        return {
            "id": "fuel_inactive",
            "band": None,
            "label": "No active fuel guidance",
            "range": None,
            "preferred": None,
            "pilot_only": False,
            "advisory": True,
            "detail": "The roast has dropped; Peak Roasting does not sense or apply a fuel setting.",
            "schedule_note": None,
        }

    degrees_to_target = None if bt is None else float(drop_target_f) - float(bt)
    seconds_to_drop = prediction.get("seconds_to_drop") if prediction else None
    immediate_dump = recording and (
        (degrees_to_target is not None and degrees_to_target <= 5)
        or (seconds_to_drop is not None and seconds_to_drop <= 8)
    )
    final_approach = recording and (
        immediate_dump
        or (degrees_to_target is not None and degrees_to_target <= 15)
        or (seconds_to_drop is not None and seconds_to_drop <= 20)
    )
    if profile_key == "correction":
        immediate_dump = recording and (
            (elapsed is not None and elapsed >= 295) or (bt is not None and bt >= 373)
        )
        final_approach = recording and (
            immediate_dump or (elapsed is not None and elapsed >= 270) or (bt is not None and bt >= 370)
        )

    if immediate_dump:
        guidance_id, band, label, fuel_range, preferred, pilot_only = (
            "fuel_pilot_only", "pilot_only", "Pilot Only", None, None, True
        )
        phase = "Immediate dump preparation; Pilot Only is separate from the numbered fuel bands."
    elif final_approach or profile_key == "correction":
        guidance_id, band, label, fuel_range, preferred, pilot_only = (
            "fuel_low", "low", "Low", [4, 5], 5, False
        )
        phase = "Final approach."
    elif "TP" in events:
        guidance_id, band, label, fuel_range, preferred, pilot_only = (
            "fuel_mid", "mid", "Mid", [6, 7], 6, False
        )
        phase = "After the recorded turning point."
    else:
        guidance_id, band, label, fuel_range, preferred, pilot_only = (
            "fuel_high", "high", "High", [8, 8], 8, False
        )
        phase = "Until the recorded turning point."

    schedule_note = None
    if batch_g == SMALL_BATCH_G:
        schedule_note = "300 g schedule: High 8 until TP, then Mid 6 (range 6–7)."
    setting = "Pilot Only" if pilot_only else f"{label} {fuel_range[0]}–{fuel_range[1]} (preferred {preferred})"
    return {
        "id": guidance_id,
        "band": band,
        "label": label,
        "range": fuel_range,
        "preferred": preferred,
        "pilot_only": pilot_only,
        "advisory": True,
        "detail": (
            f"Advisory {setting}. {phase} Peak Roasting does not sense or apply the fuel setting."
            + (f" {schedule_note}" if schedule_note else "")
        ),
        "schedule_note": schedule_note,
    }


def build_timeline(
    *,
    recording: bool,
    auto_start_armed: bool,
    bt: float | None,
    events: dict[str, dict[str, Any]],
    prediction: dict[str, float] | None,
    fc_candidate_f: float,
    drop_target_f: float,
    drop_ceiling_f: float,
    post_drop_seconds: float | None,
    profile_key: str = "light",
    elapsed: float | None = None,
    batch_g: int | None = None,
    next_roast: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a read-only operator timeline from existing roast state and cues."""

    def remaining(value: float | None) -> float | None:
        return None if value is None else round(max(0.0, float(value)), 1)

    def step(step_id: str, label: str, reached: bool, detail: str, remaining_f=None, remaining_seconds=None):
        return {
            "id": step_id,
            "label": label,
            "_reached": reached,
            "detail": detail,
            "remaining_f": remaining(remaining_f),
            "remaining_seconds": remaining(remaining_seconds),
        }

    charged = "CHARGE" in events
    turning = "TP" in events
    dropped = "DROP" in events
    charge_detail = (
        "CHARGE derives from the recorded roast event." if charged else
        "DROP was recorded with no CHARGE event detected." if dropped else "Waiting for the recorded CHARGE event."
    )
    tp_detail = (
        "Turning point derives from the recorded TP event." if turning else
        "The turning-point window passed with no recorded TP event." if dropped else "Waiting for the recorded TP event."
    )
    guide_elapsed = max(0.0, elapsed or 0.0)
    if dropped and not charged:
        start_detail = "DROP was recorded, but the roast-start CHARGE onset was not detected."
    elif recording:
        start_detail = "Recording started; waiting for the Charge event."
    elif auto_start_armed:
        start_detail = "Automatic roast start is armed."
    else:
        start_detail = "Roast start is waiting for an armed or manual recording session."

    cooling_remaining = remaining(60 - post_drop_seconds) if dropped and post_drop_seconds is not None else None
    hopper = _hopper_copy(next_roast)
    if profile_key == "correction":
        temperature_window = bt is not None and bt >= 370
        temperature_window_remaining = None if dropped or bt is None else 370 - bt
        dump_remaining_f = None if dropped or bt is None else drop_ceiling_f - bt
        dump_remaining_seconds = None if dropped else 300 - guide_elapsed
        raw_steps = [
            step("roast_start", "Roast start", charged or dropped, start_detail),
            step("charge", "Charge", charged or dropped, charge_detail),
            step("tp", "Turning point", turning or dropped, tp_detail),
            step("air_half", "Air → 50/50", charged or dropped, "Early equalization cue; recommendation only, not sensed airflow confirmation."),
        ]
        if hopper is not None:
            raw_steps.append(step(
                "hopper_next", "Load next hopper", dropped or guide_elapsed >= 150,
                hopper[2], remaining_seconds=None if dropped else 150 - guide_elapsed,
            ))
        raw_steps.extend([
            step("air_drum", "Air → Roast Drum", dropped or guide_elapsed >= 180 or temperature_window, "180s sample-stage cue; recommendation only, not sensed airflow confirmation.", temperature_window_remaining, None if dropped else 180 - guide_elapsed),
            step(
                "air_cooling", "Air → Cooling Bin",
                dropped or guide_elapsed >= 270 or temperature_window,
                "270s or 370°F preparation cue; recommendation only, not sensed airflow confirmation.",
                temperature_window_remaining, None if dropped else 270 - guide_elapsed,
            ),
            step(
                "dump", "Pilot only + Dump", dropped,
                "Switch to pilot only for final approach; manual dump is due by 300s or the 375°F preferred ceiling.",
                dump_remaining_f, dump_remaining_seconds,
            ),
            step("cooling", "Cooling", dropped and post_drop_seconds is not None and post_drop_seconds >= 60, "Cool for 60 seconds after DROP.", remaining_seconds=cooling_remaining),
        ])
    else:
        fcs_event = events.get("FCs")
        fcs = fcs_event is not None
        fcs_detail = (
            "First crack is a manual observation milestone; it does not imply an airflow or flame change."
            if _is_manual_observation(fcs_event) else
            "First crack derives only from the recorded FCs event." if fcs else
            "The first-crack curve/listening window passed with no recorded FCs event." if dropped else
            "Curve/listening estimate only; waiting for the recorded FCs event."
        )
        prediction_seconds = prediction.get("seconds_to_drop") if prediction else None
        air_half_remaining = None if dropped or bt is None else 270 - bt
        air_drum_remaining = None if dropped or bt is None else fc_candidate_f - 10 - bt
        fcs_remaining = None if dropped or fcs or bt is None else fc_candidate_f - bt
        if dropped:
            dump_remaining_f = dump_remaining_seconds = None
        else:
            dump_remaining_f = None if bt is None else drop_target_f - bt
            dump_remaining_seconds = prediction_seconds
        raw_steps = [
            step("roast_start", "Roast start", charged or dropped, start_detail),
            step("charge", "Charge", charged or dropped, charge_detail),
            step("tp", "Turning point", turning or dropped, tp_detail),
        ]
        if dropped:
            dump_detail = f"DROP derives only from the recorded event; authoritative target {drop_target_f:g}°F."
            dump_label = "Pilot only + Dump"
        elif bt is None:
            dump_detail = f"Authoritative target {drop_target_f:g}°F; current BT delta is unavailable."
            dump_label = "Pilot only + Dump"
        elif bt >= drop_target_f:
            dump_detail = (
                f"DUMP NOW · TARGET PASSED. Authoritative target {drop_target_f:g}°F; "
                f"current delta {drop_target_f - bt:+g}°F."
            )
            dump_label = "DUMP NOW · TARGET PASSED"
        else:
            dump_detail = (
                f"Authoritative target {drop_target_f:g}°F; current delta {drop_target_f - bt:g}°F to target."
            )
            dump_label = "Pilot only + Dump"
        raw_steps.append(
            step("air_half", "Air → 50/50", dropped or (bt is not None and bt >= 270), "270°F BT cue; recommendation only, not sensed airflow confirmation.", air_half_remaining),
        )
        if hopper is not None:
            hopper_reached = dropped or (
                turning and bt is not None and drop_target_f - bt <= 60
            )
            hopper_remaining = None if dropped or bt is None else drop_target_f - 60 - bt
            raw_steps.append(step(
                "hopper_next", "Load next hopper", hopper_reached,
                hopper[2], remaining_f=hopper_remaining,
            ))
        raw_steps.extend([
            step("air_drum", "Air → Roast Drum", dropped or (bt is not None and bt >= fc_candidate_f - 10), "First-crack-near BT cue; recommendation only, not sensed airflow confirmation.", air_drum_remaining),
            step("fcs", "First crack", fcs or dropped, fcs_detail, fcs_remaining),
            step("dump", dump_label, dropped, dump_detail, dump_remaining_f, dump_remaining_seconds),
            step("cooling", "Cooling", dropped and post_drop_seconds is not None and post_drop_seconds >= 60, "Cool for 60 seconds after DROP.", remaining_seconds=cooling_remaining),
        ])

    current_index = next((index for index, item in enumerate(raw_steps) if not item["_reached"]), None)
    for index, item in enumerate(raw_steps):
        reached = item.pop("_reached")
        item["status"] = (
            "done" if reached or current_index is None or index < current_index
            else "current" if index == current_index else "upcoming"
        )
    current = raw_steps[current_index] if current_index is not None else None
    return {
        "steps": raw_steps,
        "next_step_id": current["id"] if current else None,
        "next_summary": (
            {key: current[key] for key in ("label", "remaining_f", "remaining_seconds")} if current else None
        ),
    }


def next_instruction(
    *,
    recording: bool,
    bt: float | None,
    events: dict[str, dict[str, Any]],
    prediction: dict[str, float] | None,
    airflow_current: str,
    fc_candidate_f: float,
    drop_target_f: float,
    post_drop_seconds: float | None,
    confirmed_actions: set[str],
    profile_key: str = "light",
    elapsed: float | None = None,
    ror: float | None = None,
    batch_g: int | None = None,
    next_roast: dict[str, Any] | None = None,
) -> dict[str, Any]:
    guidance = fuel_guidance(
        recording=recording,
        bt=bt,
        events=events,
        prediction=prediction,
        drop_target_f=drop_target_f,
        post_drop_seconds=post_drop_seconds,
        profile_key=profile_key,
        elapsed=elapsed,
        batch_g=batch_g,
    )
    degrees_to_target = None if bt is None else round(float(drop_target_f) - float(bt), 1)
    hopper = _hopper_copy(next_roast)

    def response(
        instruction_id: str,
        urgency: str,
        title: str,
        detail: str,
        confirm_actions: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": instruction_id,
            "urgency": urgency,
            "title": title,
            "detail": detail,
            "confirm_actions": confirm_actions or [],
            "target_temp_f": float(drop_target_f),
            "degrees_to_target_f": degrees_to_target,
            "fuel_guidance": guidance,
        }

    if post_drop_seconds is not None:
        if post_drop_seconds < 60:
            remaining = max(0, round(60 - post_drop_seconds))
            return response(
                "post_drop_cooling",
                "active",
                "KEEP COOLING · AGITATOR ON",
                f"Keep full cooling air through the Cooling Bin with the agitator on. Guided minimum: about {remaining}s remaining.",
            )
        return response(
            "post_drop_cooled",
            "ready",
            "COOLING INTERVAL COMPLETE · KEEP AGITATOR ON",
            "Continue full cooling with air through the Cooling Bin and the agitator on until satisfied or starting a new roast.",
        )

    if profile_key == "correction":
        guide_elapsed = max(0.0, elapsed or 0.0)
        final_approach = guide_elapsed >= 295 or (bt is not None and bt >= 373)
        air_cooling_prep = guide_elapsed >= 270 or (bt is not None and bt >= 370)
        confirm_actions = [
            {"id": action_id, "label": label}
            for action_id, label, available in (
                ("pilot_only", "Log pilot only", final_approach),
                ("agitator_on", "Log agitator ON", air_cooling_prep),
            )
            if recording and available and action_id not in confirmed_actions
        ]

        if not recording:
            instruction = (
                "correction_idle",
                "ready",
                "CORRECTION · PREHEAT ET 285–300°F",
                "Experimental re-roast: save a control sample before charging the already-roasted beans.",
            )
        elif bt is not None and bt >= 380:
            instruction = (
                "correction_hard_ceiling",
                "urgent",
                "DUMP NOW · 380°F ABSOLUTE LIMIT",
                "Absolute temperature limit reached.",
            )
        elif bt is not None and bt >= 375:
            instruction = (
                "correction_preferred_ceiling",
                "urgent",
                "DUMP NOW · 375°F PREFERRED CEILING",
                "Preferred temperature ceiling reached.",
            )
        elif guide_elapsed >= 300:
            instruction = (
                "correction_time_limit",
                "urgent",
                "DUMP NOW · MANUAL HARD LIMIT",
                "The five-minute correction limit is due.",
            )
        elif final_approach:
            remaining = []
            if guide_elapsed < 300:
                remaining.append(f"{round(300 - guide_elapsed, 1):g}s to the 300s limit")
            if bt is not None and bt < 375:
                remaining.append(f"{round(375 - bt, 1):g}°F to the 375°F preferred ceiling")
            instruction = (
                "correction_final_approach",
                "urgent",
                "PILOT ONLY · DUMP IN 5",
                "Switch to PILOT ONLY now" + (f"; {' · '.join(remaining)} remaining." if remaining else "."),
            )
        elif guide_elapsed >= 90 and ror is not None and ror > 15:
            instruction = (
                "correction_ror_high",
                "urgent",
                "REDUCE HEAT NOW",
                "RoR is above 15°F/min; dial the gas down immediately while maintaining controlled heat.",
            )
        elif bt is not None and bt >= 370:
            instruction = (
                "correction_temp_window",
                "active",
                "370°F WINDOW · COOLING + AGITATOR",
                "Move air to Cooling Bin, turn agitator ON, and judge color and aroma inside the 370–375°F correction window.",
            )
        elif guide_elapsed >= 270:
            instruction = (
                "correction_prepare_dump",
                "urgent",
                "PREPARE TO DUMP · COOLING + AGITATOR",
                "Move air to Cooling Bin and turn agitator ON. Keep controlled heat until the final PILOT ONLY cue.",
            )
        elif guide_elapsed >= 210:
            instruction = (
                "correction_window",
                "active",
                "CORRECTION WINDOW · COLOR + AROMA",
                "Sample every 20–30s and judge color and aroma.",
            )
        elif guide_elapsed >= 180:
            instruction = (
                "correction_sample",
                "urgent",
                "SAMPLE NOW · EVERY 20–30s",
                "Use the trier now and continue sampling every 20–30s.",
            )
        elif hopper is not None and guide_elapsed >= 150:
            instruction = (hopper[0], "active", hopper[1], hopper[2])
        elif guide_elapsed >= 150:
            instruction = (
                "correction_pilot",
                "active",
                "KEEP CONTROLLED HEAT · PREP TRIER",
                "Maintain controlled heat and prepare the trier; do not switch to pilot only yet.",
            )
        elif guide_elapsed >= 90:
            instruction = (
                "correction_ror",
                "active",
                "TARGET ROR 8–12°F/MIN",
                "Shape the reheating rate toward 8–12°F/min.",
            )
        else:
            instruction = (
                "correction_equalize",
                "active",
                "EQUALIZE · 50 / 50 · LOW HEAT",
                "Equalize the already-roasted beans at 50/50 with low heat.",
            )

        instruction_id, urgency, title, detail = instruction
        return response(
            instruction_id,
            urgency,
            title,
            f"{detail} Do not wait for FC. Use manual Dump + save.",
            confirm_actions,
        )

    if not recording:
        if batch_g == SMALL_BATCH_G:
            return response(
                "ready_small_batch",
                "ready",
                "READY · AIR → COOLING BIN",
                "Set air through the Cooling Bin and review the advisory 300 g fuel schedule before recording.",
            )
        return response(
            "ready_set_cooling",
            "ready",
            "READY · AIR → COOLING BIN",
            "Set air through the Cooling Bin, then enter the bean name and roast level and start recording. Airflow logging is optional.",
        )

    if degrees_to_target is not None and degrees_to_target <= 0:
        passed = abs(degrees_to_target)
        position = "at" if passed == 0 else f"{passed:g}°F past"
        return response(
            "dump_target_passed",
            "urgent",
            f"DUMP NOW · TARGET PASSED · TARGET {drop_target_f:g}°F",
            f"BT is {position} the authoritative target; current delta {degrees_to_target:+g}°F. Dump now.",
        )

    prediction_seconds = prediction.get("seconds_to_drop") if prediction else None
    if (
        (prediction_seconds is not None and prediction_seconds <= 20)
        or (degrees_to_target is not None and degrees_to_target <= 15)
    ):
        seconds = None if prediction_seconds is None else max(0, round(prediction_seconds))
        immediate_dump = guidance["pilot_only"]
        title = (
            f"DUMP IN {max(0.0, degrees_to_target):g}°F · TARGET {drop_target_f:g}°F"
            if degrees_to_target is not None else f"PREPARE TO DUMP · TARGET {drop_target_f:g}°F"
        )
        detail = "Move air to Cooling Bin and turn agitator ON."
        if immediate_dump:
            detail += " Set fuel to PILOT ONLY for immediate dump preparation."
        else:
            detail += " Hold the advisory Low band on final approach; do not switch to Pilot Only yet."
        detail += (
            f" Current delta {degrees_to_target:+g}°F."
            if degrees_to_target is not None else " Current BT delta is unavailable."
        )
        if seconds is not None:
            detail += f" Estimated {seconds}s."
        detail += " Action logging below is optional."
        optional_logs = [("agitator_on", "Log agitator ON")]
        if immediate_dump:
            optional_logs.append(("pilot_only", "Log pilot only"))
        return response(
            "predump_ready",
            "urgent" if immediate_dump else "active",
            title,
            detail,
            [
                {"id": action_id, "label": label}
                for action_id, label in optional_logs
                if action_id not in confirmed_actions
            ],
        )

    if (
        hopper is not None
        and "TP" in events
        and degrees_to_target is not None
        and 30 < degrees_to_target <= 60
    ):
        return response(hopper[0], "active", hopper[1], hopper[2])

    fcs_event = events.get("FCs")
    operational_fcs = fcs_event is not None and not _is_manual_observation(fcs_event)
    if operational_fcs or (bt is not None and bt >= fc_candidate_f - 10):
        return response(
            "fc_set_drum",
            "urgent",
            "MOVE AIR → ROAST DRUM NOW",
            "Increase drum airflow and listen for clustered pops. Airflow logging is optional.",
        )

    if bt is not None and bt >= 270:
        return response(
            "yellow_set_half",
            "urgent",
            "MOVE AIR → 50 / 50 NOW",
            "Yellowing threshold reached. Set the middle diverter position; airflow logging is optional.",
        )

    if bt is not None and bt >= 260:
        return response(
            "yellow_prepare",
            "prepare",
            "NEXT: AIR → 50 / 50 AT 270°F",
            "Prepare the diverter. Leave it through Cooling Bin until the prompt changes.",
        )

    return response(
        "early_set_cooling",
        "active",
        "AIR → COOLING BIN",
        "Early roast. Keep air through Cooling Bin; airflow logging is optional. Next move is 50/50 near 270°F BT.",
    )
