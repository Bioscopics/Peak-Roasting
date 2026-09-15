#!/usr/bin/env python3
"""Offline loopback server and Phidget recorder for Peak Roasting."""

from __future__ import annotations

import csv
import json
import os
import signal
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DATA_ROOT = ROOT / "data"
ROASTS_ROOT = DATA_ROOT / "roasts"
LOGS_ROOT = DATA_ROOT / "logs"
REFERENCE_PATH = ROOT / "reference-roast.json"
VENDOR_ROOT = ROOT.parent / "phidget-offline-diagnostics" / "vendor"
MANUAL_ACTION_IDS = {"agitator_on", "pilot_only", "agitator_off", "beans_spread"}
PHIDGET_CHANNEL_CURVES = {0: "bt", 1: "et"}
PRECHARGE_MAX_SAMPLES = 600
AUTO_CHARGE_MIN_PEAK_F = 250.0
AUTO_CHARGE_BASELINE_STEPS = 5
AUTO_CHARGE_REQUIRED_RISING_STEPS = 4
AUTO_CHARGE_FALLING_STEPS = 4
AUTO_CHARGE_MIN_FALL_PER_STEP_F = 2.0
AUTO_CHARGE_MIN_TOTAL_DROP_F = 8.0
AUTO_CHARGE_MAX_BASELINE_DRIFT_PER_STEP_F = 2.0
REPLAY_EVENT_ROUNDING_TOLERANCE_SECONDS = 0.01
ORIGIN_SUGGESTION_SEEDS = (
    "Brazil", "Colombia", "Costa Rica", "El Salvador", "Ethiopia", "Guatemala", "Honduras", "Indonesia",
    "Kenya", "Mexico", "Nicaragua", "Panama", "Papua New Guinea", "Peru", "Rwanda", "Sumatra", "Tanzania",
    "Uganda", "Burundi",
)
BEAN_SUGGESTION_SEEDS = (
    "Bourbon", "Typica", "Caturra", "Catuai", "Catimor", "Castillo", "Gesha", "Geisha", "SL28", "SL34",
    "Pacamara", "Pacas", "Maragogipe", "Mundo Novo", "Yellow Bourbon", "Red Bourbon", "Heirloom", "Java",
    "Batian", "Ruiru 11",
)
sys.path.insert(0, str(VENDOR_ROOT))

from roast_engine import AUTOMATIC_PHYSICAL_DROP_SOURCE, EVENT_ORDER, IR5_DOCUMENTED_MIN_BATCH_G, RoastAnalyzer, batch_guidance, effective_profile, profile_payloads  # noqa: E402
from reference_library import ReferenceLibrary  # noqa: E402
from operation_guide import build_timeline, next_instruction  # noqa: E402
from learning_profiles import (  # noqa: E402
    build_learning_block,
    learning_status,
    match_kind,
    profile_summary,
    saved_roast_summaries,
    with_color_confirmation,
    with_observed_drop,
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _canonical_label(value: object, canonical_values: tuple[str, ...] | list[str]) -> str:
    cleaned = " ".join(str(value).split())
    for canonical in canonical_values:
        if cleaned.casefold() == canonical.casefold():
            return canonical
    return cleaned.title()


class Companion:
    def __init__(self) -> None:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        ROASTS_ROOT.mkdir(parents=True, exist_ok=True)
        LOGS_ROOT.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.meta = {
            "bean": "", "origin": "Rwanda", "level": "light", "notes": "", "batch_g": None,
            "desired_drop_f": None, "teach_mode": False, "roast_level_label": "",
            "learned_profile_id": None,
        }
        self.roast_queue: list[dict] = []
        saved_roasts = self._saved_roasts()
        self.origin_suggestions = self._metadata_suggestions(saved_roasts, "origin", ORIGIN_SUGGESTION_SEEDS)
        self.bean_suggestions = self._metadata_suggestions(saved_roasts, "bean", BEAN_SUGGESTION_SEEDS)
        self._load_roast_train()
        self.analyzer = RoastAnalyzer("light", self.meta["batch_g"])
        self.recording = False
        self.recording_armed = False
        self.started_monotonic: float | None = None
        self.connected = False
        self.connection_message = "Connecting to Phidget 1048…"
        self.temperatures = {"et": None, "bt": None}
        self.sensors = []
        self.stop = threading.Event()
        self.replaying = False
        self.replay_source: dict | None = None
        self.last_saved: str | None = None
        self.viewing_history = False
        self.viewed_charge_guidance: dict | None = None
        self.log_path = LOGS_ROOT / f"companion_{datetime.now():%Y%m%d}.jsonl"
        self.reference = json.loads(REFERENCE_PATH.read_text()) if REFERENCE_PATH.exists() else {"points": [], "events": []}
        self.references = ReferenceLibrary(DATA_ROOT / "references", REFERENCE_PATH, ROOT / "bundled-references")
        self.saved_reference_roast_ids: list[str] = []
        self.audio_features: list[dict] = []
        self.manual_history: list[tuple[str, dict | None]] = []
        self.feedback_path = DATA_ROOT / "calibration" / "feedback.jsonl"
        self.feedback_path.parent.mkdir(parents=True, exist_ok=True)
        self.airflow = {"current": "unknown", "history": []}
        self.operator_actions = {"confirmed": [], "history": []}
        self.dropped_monotonic: float | None = None
        self.armed_started_monotonic: float | None = None
        self.precharge_capture: list[tuple[float, float, float]] = []
        self._restore_drop_result()
        threading.Thread(target=self._connect_phidget, daemon=True).start()
        threading.Thread(target=self._sampler, daemon=True).start()

    def _saved_roasts(self) -> list[tuple[str, dict]]:
        saved = []
        for folder in sorted(ROASTS_ROOT.iterdir(), key=lambda path: path.name, reverse=True):
            roast_path = folder / "roast.json"
            if not folder.is_dir() or not roast_path.is_file():
                continue
            try:
                payload = json.loads(roast_path.read_text())
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                saved.append((folder.name, payload))
        return saved

    def _load_saved_roast(self, roast_id: object) -> dict:
        if not isinstance(roast_id, str) or not roast_id or Path(roast_id).name != roast_id:
            raise ValueError("Invalid roast history id")
        folder = ROASTS_ROOT / roast_id
        roast_path = folder / "roast.json"
        if not folder.is_dir() or folder.resolve().parent != ROASTS_ROOT.resolve() or not roast_path.is_file():
            raise ValueError("Invalid roast history id")
        try:
            payload = json.loads(roast_path.read_text())
            if not isinstance(payload, dict) or not isinstance(payload["meta"], dict):
                raise TypeError

            def points(rows: object) -> list[dict]:
                if not isinstance(rows, list):
                    raise TypeError
                normalized = [
                    {**point, "elapsed": float(point["elapsed"]), "et": float(point["et"]), "bt": float(point["bt"])}
                    for point in rows if isinstance(point, dict)
                ]
                if len(normalized) != len(rows) or any(right["elapsed"] < left["elapsed"] for left, right in zip(normalized, normalized[1:])):
                    raise TypeError
                return normalized

            events = payload.get("events", [])
            if not isinstance(events, list) or any(not isinstance(event, dict) or event.get("name") not in EVENT_ORDER for event in events):
                raise TypeError
            normalized_events = [
                {**event, "elapsed": float(event["elapsed"]), "et": float(event["et"]), "bt": float(event["bt"])}
                for event in events
            ]
            payload = {
                **payload,
                "points": points(payload["points"]),
                "precharge_points": points(payload.get("precharge_points", [])),
                "events": normalized_events,
            }
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise ValueError("Saved roast data is invalid") from None
        return {"id": roast_id, "payload": payload, "precharge": payload["precharge_points"], "points": payload["points"]}

    @staticmethod
    def _metadata_suggestions(saved_roasts: list[tuple[str, dict]], key: str, seeds: tuple[str, ...]) -> list[str]:
        suggestions = []
        for _roast_id, payload in saved_roasts:
            value = payload.get("meta", {}).get(key) if isinstance(payload.get("meta"), dict) else None
            if isinstance(value, str):
                value = _canonical_label(value, (*seeds, *suggestions))
                if value and value.casefold() not in {item.casefold() for item in suggestions}:
                    suggestions.append(value)
        for value in seeds:
            if value.casefold() not in {item.casefold() for item in suggestions}:
                suggestions.append(value)
        return suggestions

    @staticmethod
    def _roast_summary(roast_id: str, payload: dict) -> dict:
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        bean = str(meta.get("bean", "")).strip()
        origin = str(meta.get("origin", "")).strip()
        return {
            "id": roast_id,
            "label": " · ".join(value for value in (origin, bean) if value) or roast_id,
            "bean": bean,
            "origin": origin,
            "level": meta.get("level", ""),
            "batch_g": meta.get("batch_g"),
            "saved_at": payload.get("saved_at", ""),
        }

    def log(self, event: str, **details: object) -> None:
        record = {"timestamp": now_iso(), "event": event, **details}
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")

    def _connect_phidget(self) -> None:
        try:
            from Phidget22.Devices.TemperatureSensor import TemperatureSensor
            from Phidget22.ThermocoupleType import ThermocoupleType
        except Exception as exc:
            with self.lock:
                self.connection_message = f"Phidget library unavailable: {exc}"
            self.log("PHIDGET_IMPORT_ERROR", error=str(exc), traceback=traceback.format_exc())
            return

        states = {0: None, 1: None}
        sensors = []
        for channel in (0, 1):
            sensor = TemperatureSensor()
            sensor.setDeviceSerialNumber(422272)
            sensor.setChannel(channel)

            def on_attach(ph, ch=channel):
                try:
                    ph.setThermocoupleType(ThermocoupleType.THERMOCOUPLE_TYPE_K)
                    ph.setDataInterval(max(250, ph.getMinDataInterval()))
                    with self.lock:
                        states[ch] = True
                        self.connected = all(states.values())
                        self.connection_message = "Phidget 1048 connected" if self.connected else "Opening temperature channels…"
                    self.log("CHANNEL_ATTACH", channel=ch, serial=ph.getDeviceSerialNumber())
                except Exception as exc:
                    self.log("CHANNEL_CONFIG_ERROR", channel=ch, error=str(exc))

            def on_detach(_ph, ch=channel):
                with self.lock:
                    states[ch] = False
                    self.connected = False
                    self.connection_message = f"Temperature channel {ch} disconnected"
                self.log("CHANNEL_DETACH", channel=ch)

            def on_error(_ph, code, description, ch=channel):
                with self.lock:
                    self.connection_message = f"Channel {ch}: {description}"
                self.log("CHANNEL_ERROR", channel=ch, code=code, description=description)

            def on_temperature(_ph, temperature, ch=channel):
                with self.lock:
                    key = PHIDGET_CHANNEL_CURVES[ch]
                    self.temperatures[key] = round(float(temperature) * 9 / 5 + 32, 3)

            sensor.setOnAttachHandler(on_attach)
            sensor.setOnDetachHandler(on_detach)
            sensor.setOnErrorHandler(on_error)
            sensor.setOnTemperatureChangeHandler(on_temperature)
            try:
                sensor.open()
            except Exception as exc:
                self.log("CHANNEL_OPEN_ERROR", channel=channel, error=str(exc))
            sensors.append(sensor)
        self.sensors = sensors
        self.log("PHIDGET_OPEN_REQUEST", serial=422272, channel_curves=PHIDGET_CHANNEL_CURVES)
        if not self.stop.wait(6):
            with self.lock:
                if not self.connected:
                    self.connection_message = "Phidget unavailable — close Artisan and check USB"
                    self.log("PHIDGET_ATTACH_TIMEOUT")

    def _sampler(self) -> None:
        while not self.stop.wait(1.0):
            with self.lock:
                et, bt = self.temperatures["et"], self.temperatures["bt"]
                if et is None or bt is None:
                    continue
                now = time.monotonic()
                if not self.recording:
                    if not self._auto_start_armed():
                        continue
                    self._process_charge_sample(now, et, bt)
                    continue
                if self.replaying or self.started_monotonic is None:
                    continue
                elapsed = now - self.started_monotonic
                had_drop = "DROP" in self.analyzer.events
                self.analyzer.add_point(elapsed, et, bt)
                if not had_drop and self.analyzer.events.get("DROP", {}).get("source") == AUTOMATIC_PHYSICAL_DROP_SOURCE:
                    self._finalize_recording(automatic=True)

    def _process_charge_sample(self, sample_time: float, et: float, bt: float, *, preserve_replay: bool = False) -> dict | None:
        self.precharge_capture.append((sample_time, et, bt))
        self.precharge_capture = self.precharge_capture[-PRECHARGE_MAX_SAMPLES:]
        if len(self.precharge_capture) < AUTO_CHARGE_BASELINE_STEPS + AUTO_CHARGE_FALLING_STEPS + 1:
            return None
        falling = self.precharge_capture[-(AUTO_CHARGE_FALLING_STEPS + 1):]
        falling_steps = [current[2] - previous[2] for previous, current in zip(falling, falling[1:])]
        if not all(delta <= -AUTO_CHARGE_MIN_FALL_PER_STEP_F for delta in falling_steps):
            return None

        peak_index = max(
            range(len(self.precharge_capture)),
            key=lambda index: (self.precharge_capture[index][2], self.precharge_capture[index][0]),
        )
        onset_index = peak_index
        if peak_index >= AUTO_CHARGE_REQUIRED_RISING_STEPS:
            baseline = self.precharge_capture[
                peak_index - AUTO_CHARGE_REQUIRED_RISING_STEPS:peak_index + 1
            ]
            rising_steps = sum(current[2] - previous[2] > 0 for previous, current in zip(baseline, baseline[1:]))
        else:
            rising_steps = 0

        if rising_steps < AUTO_CHARGE_REQUIRED_RISING_STEPS:
            # Back-to-back roasts can be armed while the hot probe is still cooling.
            # Use the last slow-cooling point before the sustained fall as Charge.
            onset_index = len(self.precharge_capture) - AUTO_CHARGE_FALLING_STEPS - 1
            if onset_index < AUTO_CHARGE_BASELINE_STEPS:
                return None
            baseline = self.precharge_capture[onset_index - AUTO_CHARGE_BASELINE_STEPS:onset_index + 1]
            baseline_steps = [current[2] - previous[2] for previous, current in zip(baseline, baseline[1:])]
            if not all(abs(delta) < AUTO_CHARGE_MAX_BASELINE_DRIFT_PER_STEP_F for delta in baseline_steps):
                return None

        onset_time, onset_et, onset_bt = self.precharge_capture[onset_index]
        if onset_bt < AUTO_CHARGE_MIN_PEAK_F or onset_bt - bt < AUTO_CHARGE_MIN_TOTAL_DROP_F:
            return None
        preload = self.precharge_capture[onset_index:]
        self._begin_recording(onset_time)
        if preserve_replay:
            self.replaying = True
            self._restore_drop_result(self.replay_source["payload"] if self.replay_source else None)
        charge = self.analyzer.mark_event(
            "CHARGE", elapsed=0, et=onset_et, bt=onset_bt, confidence=0.95, source="automatic BT drop"
        )
        for captured_time, captured_et, captured_bt in preload:
            self.analyzer.add_point(captured_time - onset_time, captured_et, captured_bt)
        self.log("AUTO_RECORD_START", meta=self.meta.copy(), charge=charge)
        return charge

    def _meta_candidate(self, payload: dict, base: dict, *, allow_learned_profile: bool = False) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Metadata must be an object")
        candidate = {
            "bean": "", "origin": "", "level": "light", "notes": "", "batch_g": None,
            "desired_drop_f": None, "teach_mode": False, "roast_level_label": "",
            "learned_profile_id": None, **base,
        }
        if "batch_g" in payload:
            raw = payload["batch_g"]
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                candidate["batch_g"] = None
            elif isinstance(raw, bool) or not (
                isinstance(raw, int) or (isinstance(raw, str) and raw.strip().isdigit())
            ):
                raise ValueError("Batch size must be a whole number from 1 to 5000 g")
            else:
                candidate["batch_g"] = int(raw)
            if candidate["batch_g"] is not None and not 1 <= candidate["batch_g"] <= 5000:
                raise ValueError("Batch size must be a whole number from 1 to 5000 g")
        if "level" in payload:
            if payload["level"] not in profile_payloads():
                raise ValueError("Unknown roast level")
            candidate["level"] = payload["level"]
        if "desired_drop_f" in payload:
            raw = payload["desired_drop_f"]
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                candidate["desired_drop_f"] = None
            elif isinstance(raw, bool):
                raise ValueError("Desired drop temperature must be a number")
            else:
                try:
                    candidate["desired_drop_f"] = float(raw)
                except (TypeError, ValueError):
                    raise ValueError("Desired drop temperature must be a number") from None
        if "bean" in payload:
            candidate["bean"] = _canonical_label(
                payload["bean"], (*BEAN_SUGGESTION_SEEDS, *self.bean_suggestions)
            )[:300]
        if "origin" in payload:
            candidate["origin"] = _canonical_label(
                payload["origin"], (*ORIGIN_SUGGESTION_SEEDS, *self.origin_suggestions)
            )[:300]
        if "notes" in payload:
            candidate["notes"] = str(payload["notes"])[:300]
        if "teach_mode" in payload:
            if not isinstance(payload["teach_mode"], bool):
                raise ValueError("Teach mode must be true or false")
            candidate["teach_mode"] = payload["teach_mode"]
        if "roast_level_label" in payload:
            candidate["roast_level_label"] = " ".join(str(payload["roast_level_label"]).split())[:300]
        if "learned_profile_id" in payload:
            value = payload["learned_profile_id"]
            if value is not None and not isinstance(value, str):
                raise ValueError("Learned profile id must be text or null")
            if value and value != base.get("learned_profile_id") and not allow_learned_profile:
                raise ValueError("Use learned-profile selection to choose a learned profile")
            candidate["learned_profile_id"] = value or None
        effective_profile(candidate["level"], candidate["batch_g"], candidate["desired_drop_f"])
        return candidate

    def _remember_meta_suggestions(self, candidate: dict, payload: dict) -> None:
        for key, suggestions in (("origin", self.origin_suggestions), ("bean", self.bean_suggestions)):
            value = candidate[key]
            if key in payload and value.strip():
                suggestions[:] = [value] + [item for item in suggestions if item.casefold() != value.casefold()]

    @property
    def next_meta(self) -> dict | None:
        return self.roast_queue[0]["meta"] if self.roast_queue else None

    @property
    def next_reference_roast_id(self) -> str | None:
        return self.roast_queue[0]["reference_roast_id"] if self.roast_queue else None

    def _validated_queue_reference(self, candidate: dict, reference_id: object) -> tuple[dict, str | None]:
        learned_profile_id = candidate.get("learned_profile_id")
        if not learned_profile_id or not isinstance(reference_id, str) or not reference_id:
            if learned_profile_id:
                candidate = {**candidate, "learned_profile_id": None}
            return candidate, None
        summary = next(
            (
                item for item in saved_roast_summaries(self._saved_roasts())
                if item.get("source_roast_id") == reference_id
                and item.get("profile_id") == learned_profile_id
            ),
            None,
        )
        if not summary or match_kind(summary, candidate) != "exact":
            return {**candidate, "learned_profile_id": None}, None
        return candidate, reference_id

    def _commit_roast_queue(self, queue: list[dict]) -> None:
        path = DATA_ROOT / "roast_train.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({
            "version": 1,
            "queue": [
                {
                    "id": entry["id"],
                    "meta": entry["meta"],
                    "reference_roast_id": entry.get("reference_roast_id"),
                }
                for entry in queue
            ],
        }, indent=2) + "\n")
        os.replace(temporary, path)
        self.roast_queue = queue

    def _load_roast_train(self) -> None:
        path = DATA_ROOT / "roast_train.json"
        if not path.is_file():
            return
        try:
            stored = json.loads(path.read_text())
            rows = stored.get("queue") if isinstance(stored, dict) else None
            if not isinstance(rows, list):
                return
            queue = []
            seen_ids = set()
            for row in rows[:50]:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    continue
                entry_id = row["id"].strip()
                if not entry_id or entry_id in seen_ids or not isinstance(row.get("meta"), dict):
                    continue
                candidate = self._meta_candidate(row["meta"], self.meta, allow_learned_profile=True)
                candidate, reference_id = self._validated_queue_reference(
                    candidate, row.get("reference_roast_id")
                )
                queue.append({"id": entry_id, "meta": candidate, "reference_roast_id": reference_id})
                seen_ids.add(entry_id)
                self._remember_meta_suggestions(candidate, {"bean": candidate["bean"], "origin": candidate["origin"]})
            self.roast_queue = queue
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            self.roast_queue = []

    def _queue_state(self) -> list[dict]:
        return [
            {
                "id": entry["id"],
                "position": position,
                "meta": entry["meta"].copy(),
                "profile": asdict(effective_profile(
                    entry["meta"]["level"], entry["meta"]["batch_g"], entry["meta"]["desired_drop_f"]
                )),
                "charge_guidance": self._charge_guidance(entry["meta"]),
                "reference_roast_id": entry.get("reference_roast_id"),
            }
            for position, entry in enumerate(self.roast_queue, 1)
        ]

    def update_meta(self, payload: dict) -> None:
        with self.lock:
            if self.recording_armed or self.recording or self.replaying or self.last_saved or self.viewing_history:
                raise ValueError("Active roast metadata is read-only; edit Next Roast instead")
            candidate = self._meta_candidate(payload, self.meta)
            self.analyzer.set_profile(candidate["level"], candidate["batch_g"], candidate["desired_drop_f"])
            self.meta = candidate
            self._remember_meta_suggestions(candidate, payload)
            self.log("META_UPDATE", meta=self.meta.copy())

    def update_next_meta(self, payload: dict) -> None:
        with self.lock:
            candidate = self._meta_candidate(payload, self.next_meta or self.meta)
            queued_reference = self.next_reference_roast_id
            candidate, queued_reference = self._validated_queue_reference(candidate, queued_reference)
            entry_id = self.roast_queue[0]["id"] if self.roast_queue else f"train-{uuid.uuid4().hex}"
            head = {"id": entry_id, "meta": candidate, "reference_roast_id": queued_reference}
            self._commit_roast_queue([head, *self.roast_queue[1:]])
            self._remember_meta_suggestions(candidate, payload)
            self.log("NEXT_META_UPDATE", next_meta=candidate.copy())

    def add_queue_item(self, payload: dict) -> dict:
        with self.lock:
            if len(self.roast_queue) >= 50:
                raise ValueError("Roast train is limited to 50 queued roasts")
            base = {**self.meta, "learned_profile_id": None}
            candidate = self._meta_candidate(payload, base)
            entry = {
                "id": f"train-{uuid.uuid4().hex}",
                "meta": candidate,
                "reference_roast_id": None,
            }
            self._commit_roast_queue([*self.roast_queue, entry])
            self._remember_meta_suggestions(candidate, payload)
            self.log("ROAST_QUEUE_ADD", queue_id=entry["id"], meta=candidate.copy())
            return self.state()

    def update_queue_item(self, payload: dict) -> dict:
        with self.lock:
            entry_id = payload.get("id") if isinstance(payload, dict) else None
            meta_payload = payload.get("meta") if isinstance(payload, dict) else None
            if not isinstance(entry_id, str) or not isinstance(meta_payload, dict):
                raise ValueError("Queue update requires an id and metadata object")
            index = next((i for i, entry in enumerate(self.roast_queue) if entry["id"] == entry_id), None)
            if index is None:
                raise ValueError("Queued roast was not found")
            existing = self.roast_queue[index]
            candidate = self._meta_candidate(meta_payload, existing["meta"])
            candidate, reference_id = self._validated_queue_reference(
                candidate, existing.get("reference_roast_id")
            )
            queue = self.roast_queue.copy()
            queue[index] = {"id": entry_id, "meta": candidate, "reference_roast_id": reference_id}
            self._commit_roast_queue(queue)
            self._remember_meta_suggestions(candidate, meta_payload)
            self.log("ROAST_QUEUE_UPDATE", queue_id=entry_id, meta=candidate.copy())
            return self.state()

    def remove_queue_item(self, payload: dict) -> dict:
        with self.lock:
            entry_id = payload.get("id") if isinstance(payload, dict) else None
            index = next((i for i, entry in enumerate(self.roast_queue) if entry["id"] == entry_id), None)
            if not isinstance(entry_id, str) or index is None:
                raise ValueError("Queued roast was not found")
            self._commit_roast_queue(self.roast_queue[:index] + self.roast_queue[index + 1:])
            self.log("ROAST_QUEUE_REMOVE", queue_id=entry_id)
            return self.state()

    def move_queue_item(self, payload: dict) -> dict:
        with self.lock:
            entry_id = payload.get("id") if isinstance(payload, dict) else None
            direction = payload.get("direction") if isinstance(payload, dict) else None
            if not isinstance(entry_id, str) or type(direction) is not int or direction not in (-1, 1):
                raise ValueError("Queue move requires an id and direction -1 or 1")
            index = next((i for i, entry in enumerate(self.roast_queue) if entry["id"] == entry_id), None)
            if index is None:
                raise ValueError("Queued roast was not found")
            destination = index + direction
            if destination < 0 or destination >= len(self.roast_queue):
                raise ValueError("Queued roast cannot move past the end of the train")
            queue = self.roast_queue.copy()
            queue[index], queue[destination] = queue[destination], queue[index]
            self._commit_roast_queue(queue)
            self.log("ROAST_QUEUE_MOVE", queue_id=entry_id, position=destination + 1)
            return self.state()

    def _begin_recording(self, started_monotonic: float) -> None:
        self.analyzer = RoastAnalyzer(self.meta["level"], self.meta["batch_g"], self.meta["desired_drop_f"])
        self._restore_drop_result()
        self.audio_features = []
        self.manual_history = []
        self.operator_actions = {"confirmed": [], "history": []}
        self.recording_armed = False
        self.recording = True
        self.replaying = False
        self.started_monotonic = started_monotonic
        self.last_saved = None
        self.viewing_history = False
        self.viewed_charge_guidance = None
        self.dropped_monotonic = None

    def _auto_start_armed(self) -> bool:
        return self.recording_armed and self.connected and not self.recording and not self.replaying and self.last_saved is None and self.dropped_monotonic is None

    def start_recording(self) -> None:
        with self.lock:
            if not self.connected:
                raise ValueError("Phidget is not connected. Close Artisan, connect the USB hub, and wait for the green status.")
            if self.recording or self.replaying or self.last_saved is not None or self.dropped_monotonic is not None:
                return
            if self.recording_armed:
                return
            if self.meta["teach_mode"] and not self.meta["roast_level_label"].strip():
                raise ValueError("Roast level label is required to arm a Teach roast")
            now = time.monotonic()
            self.recording_armed = True
            self._restore_drop_result()
            self.armed_started_monotonic = now
            self.precharge_capture = []
            et, bt = self.temperatures["et"], self.temperatures["bt"]
            if et is not None and bt is not None:
                self.precharge_capture.append((now, et, bt))
            self.log("RECORD_ARM", meta=self.meta.copy())

    def cancel_arm(self) -> dict:
        with self.lock:
            if not self.recording_armed or self.recording or self.replaying:
                raise ValueError("No armed capture is available to cancel")
            self.recording_armed = False
            self.started_monotonic = None
            self.armed_started_monotonic = None
            self.precharge_capture.clear()
            self.log("RECORD_ARM_CANCEL")
            return self.state()

    def _reset_ready(self, *, promote_next: bool) -> None:
        if self.recording or self.replaying:
            raise ValueError("Stop the active roast or replay before starting a new roast")
        promoted_entry = self.roast_queue[0] if promote_next and self.roast_queue else None
        promoted = promoted_entry["meta"].copy() if promoted_entry else None
        queued_reference = promoted_entry.get("reference_roast_id") if promoted_entry else None
        prepared_reference = None
        if queued_reference:
            source = self._load_saved_roast(queued_reference)
            prepared_reference = self.references.saved_reference([(source["id"], source["payload"])])
        if promoted:
            self._commit_roast_queue(self.roast_queue[1:])
            self.meta = promoted
        self.analyzer = RoastAnalyzer(self.meta["level"], self.meta["batch_g"], self.meta["desired_drop_f"])
        self._restore_drop_result()
        self.recording_armed = False
        self.started_monotonic = None
        self.audio_features = []
        self.manual_history = []
        self.operator_actions = {"confirmed": [], "history": []}
        self.airflow["history"] = []
        self.last_saved = None
        self.viewing_history = False
        self.viewed_charge_guidance = None
        self.dropped_monotonic = None
        self.armed_started_monotonic = None
        self.precharge_capture.clear()
        self.replay_source = None
        if prepared_reference:
            self.references.activate_saved(prepared_reference)
            self.saved_reference_roast_ids = [queued_reference]
            self.log("SAVED_REFERENCE_ACTIVE", roast_ids=[queued_reference], reference=prepared_reference["id"])

    def new_roast(self) -> None:
        with self.lock:
            self._reset_ready(promote_next=True)
            self.log("NEW_ROAST_READY", meta=self.meta.copy())

    def mark(self, name: str) -> dict:
        with self.lock:
            if name not in EVENT_ORDER:
                raise ValueError("Unknown event")
            if name == "CHARGE" and self._auto_start_armed():
                now = time.monotonic()
                et, bt = self.temperatures["et"], self.temperatures["bt"]
                if et is None or bt is None:
                    if not self.precharge_capture:
                        raise ValueError("No temperature sample yet")
                    _sample_time, et, bt = self.precharge_capture[-1]
                self.precharge_capture.append((now, et, bt))
                self.precharge_capture = self.precharge_capture[-PRECHARGE_MAX_SAMPLES:]
                self._begin_recording(now)
                event = self.analyzer.mark_event(
                    "CHARGE", elapsed=0, et=et, bt=bt, confidence=1.0, source="manual beans in"
                )
                self.analyzer.add_point(0, et, bt, auto=False)
                self.log("MANUAL_RECORD_START", meta=self.meta.copy(), charge=event)
                return event
            if not self.analyzer.points:
                raise ValueError("No temperature sample yet")
            p = self.analyzer.points[-1]
            previous = self.analyzer.events.get(name)
            self.manual_history.append((name, previous.copy() if previous else None))
            event = self.analyzer.mark_event(name, **p, confidence=1.0, source="manual")
            if name == "DROP":
                self.dropped_monotonic = time.monotonic()
            if name not in {"FCs", "FCe", "SCs", "SCe"}:
                self.analyzer._add_alert("manual", f"{name} marked", p["elapsed"], "stage")
            self.log("EVENT_MARK", marked_event=event)
            return event

    def undo_manual(self) -> dict:
        with self.lock:
            if not self.manual_history:
                raise ValueError("There is no manual mark to undo")
            name, previous = self.manual_history.pop()
            if previous is None:
                self.analyzer.events.pop(name, None)
            else:
                self.analyzer.events[name] = previous
            self.log("EVENT_UNDO", name=name, restored=previous)
            return {"name": name, "restored": previous}

    def stop_recording(self) -> str:
        with self.lock:
            if self.replaying:
                raise ValueError("Cancel the replay before saving a roast")
            if not self.recording and not self.last_saved:
                raise ValueError("There is no active recording to save")
            return self._finalize_recording(automatic=False)

    def _finalize_recording(self, *, automatic: bool) -> str:
        with self.lock:
            self.recording_armed = False
            if not self.recording and self.last_saved:
                return self.last_saved
            self.recording = False
            self.replaying = False
            self.dropped_monotonic = time.monotonic()
            if self.analyzer.points and "DROP" not in self.analyzer.events:
                p = self.analyzer.points[-1]
                self.analyzer.mark_event("DROP", **p, confidence=1.0, source="Stop / Dump button")
            self.analyzer.backfill()
            self.confirm_operator_action("dump", allow_dump=True)
            roast_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.last_saved = roast_id
            self._save(roast_id)
            self.log(
                "AUTO_DROP_FINALIZE" if automatic else "RECORD_STOP",
                roast_id=roast_id,
                drop_event=self.analyzer.events.get("DROP"),
            )
            return roast_id

    def _restore_drop_result(self, payload: dict | None = None) -> None:
        result = payload.get("drop_result") if isinstance(payload, dict) else None
        result = result if isinstance(result, dict) else {}
        self.sensor_drop_f = result.get("sensor_drop_f")
        self.missed_drop_target = bool(result.get("missed_target", False))
        self.actual_drop_override_f = result.get("actual_override_f")

    def _drop_result(self) -> dict | None:
        drop = self.analyzer.events.get("DROP")
        if not drop:
            return None
        target = float(self.analyzer.profile.drop_target_f)
        recorded = float(drop["bt"])
        sensor = float(self.sensor_drop_f if self.sensor_drop_f is not None else recorded)
        delta = recorded - target
        within = abs(delta) <= 1.0
        return {
            "target_f": round(target, 2),
            "sensor_drop_f": round(sensor, 2),
            "recorded_drop_f": round(recorded, 2),
            "delta_f": round(delta, 2),
            "within_1f": within,
            "missed_target": self.missed_drop_target,
            "actual_override_f": self.actual_drop_override_f,
            "status": "missed_target" if self.missed_drop_target else (
                "on_target" if within else "outside_target_unconfirmed"
            ),
        }

    def record_drop_result(self, payload: dict) -> dict:
        with self.lock:
            if (
                self.recording or self.replaying or self.recording_armed or self.viewing_history
                or not self.last_saved or "DROP" not in self.analyzer.events
            ):
                raise ValueError("A completed current saved roast is required")
            if self.analyzer.profile.mode == "correction":
                raise ValueError("Correction roast drop results are read-only")
            if payload.get("missed") is not True:
                raise ValueError("Missed must be true")
            raw_actual = payload.get("actual_drop_f")
            actual = None
            if raw_actual is not None and not (isinstance(raw_actual, str) and not raw_actual.strip()):
                if isinstance(raw_actual, bool):
                    raise ValueError("Actual drop temperature must be from 250 to 500°F")
                try:
                    actual = float(raw_actual)
                except (TypeError, ValueError):
                    raise ValueError("Actual drop temperature must be from 250 to 500°F") from None
                if not 250 <= actual <= 500:
                    raise ValueError("Actual drop temperature must be from 250 to 500°F")
            drop = self.analyzer.events["DROP"]
            if self.sensor_drop_f is None:
                self.sensor_drop_f = float(drop["bt"])
            self.missed_drop_target = True
            if actual is not None:
                self.actual_drop_override_f = actual
                drop["bt"] = round(actual, 2)
                drop["source"] = "manual corrected drop temperature"
            self._save(self.last_saved)
            self.log("DROP_RESULT", roast_id=self.last_saved, drop_result=self._drop_result())
            return self.state()

    def _save(self, roast_id: str) -> None:
        folder = ROASTS_ROOT / roast_id
        folder.mkdir(parents=True, exist_ok=True)
        roast_path = folder / "roast.json"
        existing_learning = None
        if roast_path.is_file():
            try:
                stored = json.loads(roast_path.read_text())
                existing_learning = stored.get("learning") if isinstance(stored, dict) else None
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        payload = {
            "id": roast_id,
            "saved_at": now_iso(),
            "meta": self.meta.copy(),
            "audio_features": self.audio_features,
            "airflow_history": self.airflow["history"],
            "operator_action_history": self.operator_actions["history"],
            "precharge_points": self._precharge_points(),
            "charge_guidance": self._charge_guidance(),
            "drop_result": self._drop_result(),
            **self.analyzer.payload(),
        }
        if self.meta["teach_mode"]:
            learning = existing_learning if isinstance(existing_learning, dict) else build_learning_block(roast_id, payload)
            if existing_learning is not None and self.actual_drop_override_f is not None:
                learning = with_observed_drop(learning, self.actual_drop_override_f)
            payload["learning"] = learning
        roast_path.write_text(json.dumps(payload, indent=2) + "\n")
        with (folder / "temperatures.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["elapsed", "et", "bt"])
            writer.writeheader()
            writer.writerows(self.analyzer.points)

    def confirm_learning_color(self, payload: dict) -> dict:
        with self.lock:
            roast_id = payload.get("roast_id")
            if roast_id is None:
                if self.recording or self.replaying or self.recording_armed or not self.last_saved:
                    raise ValueError("A completed saved current roast or explicit roast id is required")
                roast_id = self.last_saved
            source = self._load_saved_roast(roast_id)
            roast_path = ROASTS_ROOT / source["id"] / "roast.json"
            stored = json.loads(roast_path.read_text())
            learning = stored.get("learning") if isinstance(stored, dict) else None
            if not isinstance(learning, dict):
                raise ValueError("The saved roast has no Teach learning state")
            updated = with_color_confirmation(learning, payload.get("state"), str(payload.get("notes", ""))[:500])
            stored["learning"] = updated
            temporary = roast_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(stored, indent=2) + "\n")
            os.replace(temporary, roast_path)
            self.log(
                "LEARNING_COLOR_CONFIRMATION", roast_id=source["id"],
                state=updated["color_confirmation"]["state"], status=learning_status(updated),
            )
            return self.state()

    def select_learned_profile(self, payload: dict) -> dict:
        with self.lock:
            profile_id = payload.get("profile_id")
            if not isinstance(profile_id, str) or not profile_id:
                raise ValueError("Choose a learned profile")
            saved_roasts = self._saved_roasts()
            summary = next(
                (item for item in saved_roast_summaries(saved_roasts)
                 if item.get("profile_id") == profile_id and item.get("status") == "reusable"),
                None,
            )
            if summary is None:
                raise ValueError("Learned profile is unknown or not reusable")
            source = self._load_saved_roast(summary["source_roast_id"])
            source_meta = source["payload"]["meta"]
            level = source_meta.get("level", "light")
            if level not in profile_payloads():
                level = "light"
            candidate = self._meta_candidate(
                {
                    "bean": source_meta.get("bean", ""),
                    "origin": source_meta.get("origin", ""),
                    "batch_g": source_meta.get("batch_g"),
                    "level": level,
                    "desired_drop_f": summary["learned_drop_f"],
                    "teach_mode": False,
                    "roast_level_label": source_meta.get("roast_level_label", ""),
                    "learned_profile_id": profile_id,
                },
                self.next_meta or self.meta,
                allow_learned_profile=True,
            )
            if match_kind(summary, candidate) != "exact":
                raise ValueError("Learned profile identity is invalid")
            entry_id = self.roast_queue[0]["id"] if self.roast_queue else f"train-{uuid.uuid4().hex}"
            entry = {"id": entry_id, "meta": candidate, "reference_roast_id": source["id"]}
            self._commit_roast_queue([entry, *self.roast_queue[1:]])
            self.log("LEARNED_PROFILE_QUEUED", profile_id=profile_id, roast_id=source["id"])
            return self.state()

    def view_history(self, roast_id: object) -> dict:
        with self.lock:
            if self.recording or self.replaying or self.recording_armed:
                raise ValueError("Stop the active roast, replay, or armed capture before viewing history")
            source = self._load_saved_roast(roast_id)
            payload = source["payload"]
            stored_meta, stored_points, stored_events = payload["meta"], source["points"], payload["events"]
            level = stored_meta.get("level", "light")
            if level not in profile_payloads():
                level = "light"
            desired_drop_f = stored_meta.get("desired_drop_f")
            analyzer = RoastAnalyzer(level, stored_meta.get("batch_g"), desired_drop_f)
            for point in stored_points:
                analyzer.add_point(point["elapsed"], point["et"], point["bt"], auto=False)
            analyzer.events = {
                event["name"]: event.copy()
                for event in stored_events
                if isinstance(event, dict) and event.get("name") in EVENT_ORDER
            }
            if isinstance(payload.get("alerts"), list):
                analyzer.alerts = [alert.copy() for alert in payload["alerts"] if isinstance(alert, dict)]
            self.meta = {
                "bean": str(stored_meta.get("bean", ""))[:300],
                "origin": str(stored_meta.get("origin", ""))[:300],
                "level": level,
                "notes": str(stored_meta.get("notes", ""))[:300],
                "batch_g": stored_meta.get("batch_g"),
                "desired_drop_f": desired_drop_f,
                "teach_mode": bool(stored_meta.get("teach_mode", False)),
                "roast_level_label": str(stored_meta.get("roast_level_label", ""))[:300],
                "learned_profile_id": stored_meta.get("learned_profile_id"),
            }
            self.analyzer = analyzer
            self.recording = False
            self.replaying = False
            self.recording_armed = False
            self.started_monotonic = None
            self.dropped_monotonic = None
            self.armed_started_monotonic = 0.0
            self.precharge_capture = [
                (float(point["elapsed"]), float(point["et"]), float(point["bt"]))
                for point in payload.get("precharge_points", [])
                if isinstance(point, dict) and all(key in point for key in ("elapsed", "et", "bt"))
            ]
            self.last_saved = roast_id
            self.viewing_history = True
            guidance = payload.get("charge_guidance")
            self.viewed_charge_guidance = guidance.copy() if isinstance(guidance, dict) else None
            self._restore_drop_result(payload)
            self.replay_source = None
            self.log("HISTORY_VIEW", roast_id=roast_id)
            return self.state()

    def start_replay(self, speed: float = 30.0, roast_id: object = None) -> None:
        with self.lock:
            if self.recording or self.replaying or self.recording_armed:
                raise ValueError("Stop the active roast, replay, or armed capture before replaying")
            if roast_id is not None:
                saved_source = self._load_saved_roast(roast_id)
                if not saved_source["points"]:
                    raise ValueError("Saved roast data is invalid")
            else:
                saved_source = None
                for candidate_id, _payload in self._saved_roasts():
                    try:
                        candidate = self._load_saved_roast(candidate_id)
                    except ValueError:
                        continue
                    if candidate["precharge"] and candidate["points"]:
                        saved_source = candidate
                        break
            if saved_source:
                replay_meta = saved_source["payload"].get("meta", {})
                replay_meta = replay_meta if isinstance(replay_meta, dict) else {}
                level = replay_meta.get("level", "light")
                if level not in profile_payloads():
                    level = "light"
                self.meta = {
                    "bean": str(replay_meta.get("bean", ""))[:300],
                    "origin": str(replay_meta.get("origin", ""))[:300],
                    "level": level,
                    "notes": str(replay_meta.get("notes", ""))[:300],
                    "batch_g": replay_meta.get("batch_g"),
                    "desired_drop_f": replay_meta.get("desired_drop_f"),
                    "teach_mode": bool(replay_meta.get("teach_mode", False)),
                    "roast_level_label": str(replay_meta.get("roast_level_label", ""))[:300],
                    "learned_profile_id": replay_meta.get("learned_profile_id"),
                }
            self.analyzer = RoastAnalyzer(self.meta["level"], self.meta["batch_g"], self.meta["desired_drop_f"])
            self._restore_drop_result(saved_source["payload"] if saved_source else None)
            self.operator_actions = {"confirmed": [], "history": []}
            self.replaying = True
            self.replay_source = saved_source
            self.recording = saved_source is None or not saved_source["precharge"]
            self.recording_armed = bool(saved_source and saved_source["precharge"])
            self.started_monotonic = time.monotonic() if saved_source is None else None
            self.last_saved = None
            self.viewing_history = False
            self.viewed_charge_guidance = None
            self.dropped_monotonic = None
            self.armed_started_monotonic = 0.0 if saved_source and saved_source["precharge"] else None
            self.precharge_capture.clear()
        threading.Thread(target=self._run_replay, args=(max(1, min(speed, 120)),), daemon=True).start()

    def cancel_replay(self) -> dict:
        with self.lock:
            if not self.replaying:
                raise ValueError("No replay is active")
            self.recording = False
            self.replaying = False
            self._reset_ready(promote_next=False)
            self.log("REPLAY_CANCEL")
            return self.state()

    def _precharge_points(self) -> list[dict]:
        if self.armed_started_monotonic is None:
            return []
        return [
            {"elapsed": round(sample_time - self.armed_started_monotonic, 2), "et": et, "bt": bt}
            for sample_time, et, bt in self.precharge_capture
        ]

    def _charge_guidance(self, meta: dict | None = None) -> dict:
        if meta is None and self.viewing_history and self.viewed_charge_guidance is not None:
            return self.viewed_charge_guidance.copy()
        meta = self.meta if meta is None else meta
        correction = meta["level"] == "correction"
        batch_g = meta["batch_g"]
        return {
            "sensor": "et" if correction else "bt",
            "target_f": None if correction else 415.0,
            "low_f": 285.0 if correction else 350.0,
            "high_f": 300.0 if correction else 440.0,
            "basis": "Existing correction experiment envelope" if correction else "Diedrich IR-Series starting baseline",
            "experimental_underload": batch_g is not None and batch_g < IR5_DOCUMENTED_MIN_BATCH_G,
            "batch_adjustment_applied": False,
        }

    def import_reference(self, payload: dict) -> dict:
        content = str(payload.get("content", ""))
        filename = str(payload.get("filename", "imported.alog"))
        if len(content) > 10_000_000:
            raise ValueError("Reference file is too large")
        summary = self.references.import_alog(
            content,
            filename,
            {key: str(payload.get(key, "")) for key in ("name", "origin", "level", "machine")},
        )
        self.saved_reference_roast_ids = []
        self.log("REFERENCE_IMPORT", reference=summary)
        return summary

    def set_active_reference(self, reference_id: str) -> None:
        with self.lock:
            self.references.set_active(reference_id)
            self.saved_reference_roast_ids = []

    def set_saved_reference(self, roast_ids: object) -> dict:
        with self.lock:
            if self.recording or self.replaying or self.recording_armed:
                raise ValueError("Stop the active roast, replay, or armed capture before changing references")
            if (
                not isinstance(roast_ids, list)
                or len(roast_ids) not in (1, 2)
                or any(not isinstance(roast_id, str) or not roast_id for roast_id in roast_ids)
                or len(set(roast_ids)) != len(roast_ids)
            ):
                raise ValueError("Choose one or two different saved roasts")
            sources = [self._load_saved_roast(roast_id) for roast_id in roast_ids]
            reference = self.references.saved_reference([(source["id"], source["payload"]) for source in sources])
            summary = self.references.activate_saved(reference)
            self.saved_reference_roast_ids = roast_ids.copy()
            self.log("SAVED_REFERENCE_ACTIVE", roast_ids=roast_ids, reference=summary)
            return summary

    def ingest_audio(self, payload: dict) -> dict:
        with self.lock:
            feature = {
                "timestamp": now_iso(),
                "elapsed": round(float(payload.get("elapsed", 0)), 2),
                "rms": round(float(payload.get("rms", 0)), 5),
                "baseline": round(float(payload.get("baseline", 0)), 5),
                "pops_8s": int(payload.get("pops_8s", 0)),
                "sharpness": round(float(payload.get("sharpness", 0)), 3),
            }
            self.audio_features.append(feature)
            self.audio_features = self.audio_features[-3600:]
            self.log("AUDIO_FEATURE", **feature)
            if self.recording and self.analyzer.points and self.analyzer.profile.mode != "correction":
                point = self.analyzer.points[-1]
                charge = self.analyzer.events.get("CHARGE")
                likelihood = self.references.fc_likelihood(self.analyzer.points, self.analyzer.events)
                roast_elapsed = point["elapsed"] - charge["elapsed"] if charge else 0
                if (
                    "FCs" not in self.analyzer.events
                    and charge
                    and roast_elapsed > 280
                    and feature["pops_8s"] >= 3
                    and likelihood["probability"] >= 0.28
                ):
                    event = self.analyzer.mark_event("FCs", **point, confidence=0.9, source="audio cluster + curve + references")
                    self.analyzer._add_alert("stage", "First crack detected from pop cluster", event["elapsed"], "first_crack")
                fcs = self.analyzer.events.get("FCs")
                if (
                    fcs
                    and "SCs" not in self.analyzer.events
                    and point["elapsed"] - fcs["elapsed"] > 70
                    and point["bt"] > 390
                    and feature["pops_8s"] >= 4
                    and feature["sharpness"] > 0.45
                ):
                    event = self.analyzer.mark_event("SCs", **point, confidence=0.68, source="sharp audio cluster + curve gate")
                    self.analyzer._add_alert("stage", "Second crack candidate", event["elapsed"], "warning")
            return feature

    def record_feedback(self, payload: dict) -> dict:
        outcome = str(payload.get("outcome", "")).strip().lower()
        if outcome not in {"grassy", "balanced", "roasty"}:
            raise ValueError("Choose grassy, balanced, or roasty")
        record = {
            "timestamp": now_iso(),
            "roast_id": self.last_saved or "imported-reference-20260730",
            "bean": self.meta.get("bean", ""),
            "origin": self.meta.get("origin", ""),
            "level": self.meta.get("level", ""),
            "outcome": outcome,
            "notes": str(payload.get("notes", ""))[:500],
        }
        with self.feedback_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        self.log("CUP_FEEDBACK", **record)
        return record

    def recommended_airflow(self, guide_elapsed: float) -> dict:
        prediction = self.analyzer.drop_prediction()
        point = self.analyzer.points[-1] if self.analyzer.points else None
        if self.analyzer.profile.mode == "correction":
            if "DROP" in self.analyzer.events or guide_elapsed >= 270 or (point and point["bt"] >= 370):
                return {"position": "cooling", "reason": "Correction endpoint: prepare Cooling Bin and agitator for manual dump."}
            if guide_elapsed >= 180:
                return {"position": "drum", "reason": "Correction sampling window: use Roast Drum airflow while assessing color and aroma."}
            return {"position": "half", "reason": "Correction equalization: hold 50/50 airflow through the early reheating phase."}
        if "DROP" in self.analyzer.events or (prediction and prediction["seconds_to_drop"] <= 15):
            return {"position": "cooling", "reason": "Prepare for discharge: cooling air, agitator on, flame at pilot-only."}
        fcs = self.analyzer.events.get("FCs")
        manual_fcs = str((fcs or {}).get("source", "")).strip().lower().startswith("manual")
        if (fcs and not manual_fcs) or (point and point["bt"] >= self.analyzer.profile.fc_start_f - 10):
            return {"position": "drum", "reason": "Approaching first crack: increase drum airflow to carry chaff and smoke."}
        if "DRY_END" in self.analyzer.events or (point and point["bt"] >= 270):
            return {"position": "half", "reason": "Yellowing stage: use the 50/50 air position."}
        return {"position": "cooling", "reason": "Preheat and early roast: Diedrich starts with the lever through the cooling bin."}

    def set_airflow(self, payload: dict) -> dict:
        position = str(payload.get("position", ""))
        if position not in {"cooling", "half", "drum"}:
            raise ValueError("Unknown airflow position")
        elapsed = self.analyzer.points[-1]["elapsed"] if self.analyzer.points else 0
        event = {"timestamp": now_iso(), "elapsed": round(elapsed, 2), "position": position, **self.temperatures.copy()}
        self.airflow["current"] = position
        self.airflow["history"].append(event)
        self.log("AIRFLOW_CONFIRM", **event)
        return event

    def confirm_operator_action(self, action: str, *, allow_dump: bool = False) -> dict:
        with self.lock:
            if action not in MANUAL_ACTION_IDS and not (allow_dump and action == "dump"):
                raise ValueError("Unknown operator action")
            if action in self.operator_actions["confirmed"]:
                return next(event for event in self.operator_actions["history"] if event["action"] == action)
            point = self.analyzer.points[-1] if self.analyzer.points else None
            event = {
                "action": action,
                "timestamp": now_iso(),
                "elapsed": round(point["elapsed"] if point else 0, 2),
                "et": point["et"] if point else self.temperatures["et"],
                "bt": point["bt"] if point else self.temperatures["bt"],
            }
            self.operator_actions["confirmed"].append(action)
            self.operator_actions["history"].append(event)
            self.log("OPERATOR_ACTION_CONFIRM", **event)
            if self.last_saved and not self.recording:
                self._save(self.last_saved)
            return event

    def _run_replay(self, speed: float) -> None:
        if self.replay_source is not None:
            self._run_saved_replay(speed)
            return
        points = self.reference.get("points", [])
        source_events = sorted(self.reference.get("events", []), key=lambda event: event["elapsed"])
        next_event = 0
        previous = 0.0
        self.log("REPLAY_START", speed=speed)
        for point in points:
            if self.stop.is_set() or not self.replaying:
                break
            wait = max(0, (point["elapsed"] - previous) / speed)
            if self.stop.wait(wait):
                break
            previous = point["elapsed"]
            with self.lock:
                if not self.replaying:
                    break
                self.temperatures = {"et": point["et"], "bt": point["bt"]}
                self.analyzer.add_point(point["elapsed"], point["et"], point["bt"])
                while next_event < len(source_events) and source_events[next_event]["elapsed"] <= point["elapsed"]:
                    source_event = source_events[next_event]
                    if self.analyzer.events.get(source_event["name"], {}).get("source") != "manual":
                        self.analyzer.mark_event(
                            source_event["name"],
                            elapsed=source_event["elapsed"],
                            et=source_event["et"],
                            bt=source_event["bt"],
                            confidence=source_event.get("confidence", 0.6),
                            source="original Artisan candidate",
                        )
                    next_event += 1
                if "DROP" in self.analyzer.events:
                    self.dropped_monotonic = time.monotonic()
                    break
        with self.lock:
            if self.replaying:
                self.recording = False
                self.replaying = False
                self.analyzer.backfill()
        self.log("REPLAY_END")

    def _run_saved_replay(self, speed: float) -> None:
        source = self.replay_source
        if source is None:
            return
        self.log("REPLAY_START", speed=speed, roast_id=source["id"], source="saved roast")
        previous = 0.0
        if not source["precharge"]:
            first = source["points"][0]
            with self.lock:
                if not self.replaying:
                    return
                self.analyzer.add_point(first["elapsed"], first["et"], first["bt"], auto=False)
        for point in source["precharge"]:
            if self.stop.is_set() or not self.replaying:
                break
            if self.stop.wait(max(0, (point["elapsed"] - previous) / speed)):
                break
            previous = point["elapsed"]
            with self.lock:
                if not self.replaying:
                    break
                self.temperatures = {"et": point["et"], "bt": point["bt"]}
                charge = self._process_charge_sample(
                    point["elapsed"], point["et"], point["bt"], preserve_replay=True
                )
                if charge:
                    self.log("REPLAY_CHARGE_TRIGGER", roast_id=source["id"], charge=charge)
                    break
        with self.lock:
            if not self.replaying:
                return
            if not self.recording:
                self.recording_armed = False
                self.replaying = False
                self.log("REPLAY_CHARGE_MISSED", roast_id=source["id"], samples=len(source["precharge"]))
                return

        source_events = sorted(
            (event for event in source["payload"]["events"] if not source["precharge"] or event["name"] != "CHARGE"),
            key=lambda event: event["elapsed"],
        )
        next_event = 0
        with self.lock:
            if not self.replaying:
                return
            previous = self.analyzer.points[-1]["elapsed"]
            while next_event < len(source_events) and source_events[next_event]["elapsed"] <= previous:
                event = source_events[next_event]
                self.analyzer.mark_event(
                    event["name"], elapsed=event["elapsed"], et=event["et"], bt=event["bt"],
                    confidence=event.get("confidence", 0.6), source=event.get("source", "saved roast replay"),
                )
                next_event += 1
        for point in source["points"]:
            if point["elapsed"] <= previous:
                continue
            if self.stop.is_set() or not self.replaying:
                break
            if self.stop.wait(max(0, (point["elapsed"] - previous) / speed)):
                break
            previous = point["elapsed"]
            with self.lock:
                if not self.replaying:
                    break
                self.temperatures = {"et": point["et"], "bt": point["bt"]}
                self.analyzer.add_point(point["elapsed"], point["et"], point["bt"], auto=False)
                while next_event < len(source_events) and source_events[next_event]["elapsed"] <= point["elapsed"]:
                    event = source_events[next_event]
                    self.analyzer.mark_event(
                        event["name"], elapsed=event["elapsed"], et=event["et"], bt=event["bt"],
                        confidence=event.get("confidence", 0.6), source=event.get("source", "saved roast replay"),
                    )
                    next_event += 1
                if "DROP" in self.analyzer.events:
                    self.dropped_monotonic = time.monotonic()
                    break
        with self.lock:
            if self.replaying and self.analyzer.points and self.analyzer.points[-1]["elapsed"] == source["points"][-1]["elapsed"]:
                final_elapsed = self.analyzer.points[-1]["elapsed"]
                while next_event < len(source_events) and source_events[next_event]["elapsed"] <= final_elapsed + REPLAY_EVENT_ROUNDING_TOLERANCE_SECONDS:
                    event = source_events[next_event]
                    self.analyzer.mark_event(
                        event["name"], elapsed=event["elapsed"], et=event["et"], bt=event["bt"],
                        confidence=event.get("confidence", 0.6), source=event.get("source", "saved roast replay"),
                    )
                    next_event += 1
                if "DROP" in self.analyzer.events:
                    self.dropped_monotonic = time.monotonic()
        with self.lock:
            self.recording = False
            self.recording_armed = False
            self.replaying = False
        self.log("REPLAY_END", roast_id=source["id"], source="saved roast")

    def state(self) -> dict:
        with self.lock:
            armed = self._auto_start_armed() or (self.replaying and self.recording_armed)
            elapsed = 0.0
            if self.recording and self.started_monotonic is not None and not self.replaying:
                elapsed = time.monotonic() - self.started_monotonic
            elif self.analyzer.points:
                elapsed = self.analyzer.points[-1]["elapsed"]
            point = self.analyzer.points[-1] if self.analyzer.points else None
            charge = self.analyzer.events.get("CHARGE")
            guide_elapsed = max(0.0, point["elapsed"] - charge["elapsed"]) if point and charge else elapsed
            active_reference = self.references.active()
            likelihood = self.references.fc_likelihood(self.analyzer.points, self.analyzer.events)
            airflow_recommendation = self.recommended_airflow(guide_elapsed)
            post_drop_seconds = time.monotonic() - self.dropped_monotonic if self.dropped_monotonic is not None else None
            prediction = self.analyzer.drop_prediction()
            roast_queue = self._queue_state()
            next_roast = None
            if roast_queue:
                queued = roast_queue[0]
                next_roast = {
                    "id": queued["id"],
                    "bean": queued["meta"]["bean"],
                    "origin": queued["meta"]["origin"],
                    "batch_g": queued["meta"]["batch_g"],
                    "level": queued["meta"]["level"],
                    "drop_target_f": queued["profile"]["drop_target_f"],
                }
            instruction = next_instruction(
                recording=self.recording,
                bt=point["bt"] if point else None,
                events=self.analyzer.events,
                prediction=prediction,
                airflow_current=self.airflow["current"],
                fc_candidate_f=self.analyzer.profile.fc_start_f,
                drop_target_f=self.analyzer.profile.drop_target_f,
                post_drop_seconds=post_drop_seconds,
                confirmed_actions=set(self.operator_actions["confirmed"]),
                profile_key=self.analyzer.profile.key,
                elapsed=guide_elapsed,
                ror=self.analyzer.ror,
                batch_g=self.meta.get("batch_g"),
                next_roast=next_roast,
            )
            timeline = build_timeline(
                recording=self.recording,
                auto_start_armed=armed,
                bt=point["bt"] if point else None,
                events=self.analyzer.events,
                prediction=prediction,
                fc_candidate_f=self.analyzer.profile.fc_start_f,
                drop_target_f=self.analyzer.profile.drop_target_f,
                drop_ceiling_f=self.analyzer.profile.drop_ceiling_f,
                post_drop_seconds=post_drop_seconds,
                profile_key=self.analyzer.profile.key,
                elapsed=guide_elapsed,
                batch_g=self.meta.get("batch_g"),
                next_roast=next_roast,
            )
            precharge_points = self._precharge_points()
            if self._auto_start_armed() and self.armed_started_monotonic is not None:
                capture_elapsed = time.monotonic() - self.armed_started_monotonic
            else:
                capture_elapsed = precharge_points[-1]["elapsed"] if precharge_points else 0
            saved_roasts = self._saved_roasts()
            learned_profiles = saved_roast_summaries(saved_roasts)
            pending_color_roast_id = next(
                (
                    item["source_roast_id"] for item in learned_profiles
                    if item.get("source_roast_id") == self.last_saved and item.get("status") == "pending"
                ),
                None,
            )
            next_profile = roast_queue[0]["profile"] if roast_queue else None
            return {
                "connected": self.connected,
                "connection_message": self.connection_message,
                "temperatures": self.temperatures.copy(),
                "recording": self.recording,
                "replaying": self.replaying,
                "auto_start_armed": armed,
                "elapsed": round(elapsed, 2),
                "precharge_points": precharge_points,
                "capture_elapsed": round(max(0, capture_elapsed), 2),
                "charge_guidance": self._charge_guidance(),
                "meta": self.meta.copy(),
                "next_meta": self.next_meta.copy() if self.next_meta else None,
                "next_profile": next_profile,
                "roast_queue": roast_queue,
                "origin_suggestions": self.origin_suggestions.copy(),
                "bean_suggestions": self.bean_suggestions.copy(),
                "roast_history": [self._roast_summary(roast_id, payload) for roast_id, payload in saved_roasts],
                "learned_profiles": learned_profiles,
                "selected_learned_profile_id": (
                    (self.next_meta or self.meta).get("learned_profile_id")
                ),
                "teach_session": {"pending_color_roast_id": pending_color_roast_id},
                "batch_guidance": batch_guidance(self.meta["batch_g"], self.meta["level"]),
                "profiles": profile_payloads(),
                "last_saved": self.last_saved,
                "references": self.references.summaries(),
                "active_reference_id": self.references.active_id,
                "saved_reference_roast_ids": self.saved_reference_roast_ids.copy(),
                "reference_overlay": active_reference,
                "fc_likelihood": likelihood,
                "audio": {"latest": self.audio_features[-1] if self.audio_features else None},
                "airflow": {"current": self.airflow["current"], "recommended": airflow_recommendation, "history": self.airflow["history"]},
                "operator_actions": {
                    "confirmed": self.operator_actions["confirmed"].copy(),
                    "history": self.operator_actions["history"].copy(),
                },
                "next_instruction": instruction,
                "fuel_guidance": instruction["fuel_guidance"],
                "timeline": timeline,
                "post_drop_seconds": round(post_drop_seconds, 1) if post_drop_seconds is not None else None,
                "drop_result": self._drop_result(),
                **self.analyzer.payload(),
            }

    def close(self) -> None:
        self.stop.set()
        for sensor in self.sensors:
            try:
                sensor.close()
            except Exception:
                pass


APP = Companion()


class Handler(BaseHTTPRequestHandler):
    server_version = "RoastCompanion/0.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size) or b"{}")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(APP.state())
            return
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (WEB_ROOT / relative).resolve()
        if WEB_ROOT.resolve() not in target.parents and target != WEB_ROOT.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        mime = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
            payload = self._body()
            if path == "/api/meta":
                APP.update_meta(payload)
                self._json(APP.state())
            elif path == "/api/next-meta":
                APP.update_next_meta(payload)
                self._json(APP.state())
            elif path == "/api/queue/add":
                self._json(APP.add_queue_item(payload))
            elif path == "/api/queue/update":
                self._json(APP.update_queue_item(payload))
            elif path == "/api/queue/remove":
                self._json(APP.remove_queue_item(payload))
            elif path == "/api/queue/move":
                self._json(APP.move_queue_item(payload))
            elif path == "/api/start":
                APP.update_meta(payload)
                APP.start_recording()
                self._json(APP.state())
            elif path == "/api/arm/cancel":
                self._json(APP.cancel_arm())
            elif path == "/api/new-roast":
                APP.new_roast()
                self._json(APP.state())
            elif path == "/api/history/view":
                self._json(APP.view_history(payload.get("id")))
            elif path == "/api/mark":
                self._json(APP.mark(payload.get("name", "")))
            elif path == "/api/undo":
                self._json(APP.undo_manual())
            elif path == "/api/stop":
                self._json({"roast_id": APP.stop_recording(), "state": APP.state()})
            elif path == "/api/drop-result":
                self._json(APP.record_drop_result(payload))
            elif path == "/api/learning/color-confirmation":
                self._json(APP.confirm_learning_color(payload))
            elif path == "/api/learned-profile/select":
                self._json(APP.select_learned_profile(payload))
            elif path == "/api/replay":
                explicit_roast = "roast_id" in payload
                roast_id = payload.get("roast_id")
                if explicit_roast and not isinstance(roast_id, str):
                    raise ValueError("Invalid roast history id")
                if not explicit_roast:
                    APP.update_meta(payload)
                APP.start_replay(float(payload.get("speed", 30)), roast_id)
                self._json(APP.state())
            elif path == "/api/replay/cancel":
                self._json(APP.cancel_replay())
            elif path == "/api/reference/import":
                self._json({"reference": APP.import_reference(payload), "state": APP.state()})
            elif path == "/api/reference/active":
                APP.set_active_reference(str(payload.get("id", "")))
                self._json(APP.state())
            elif path == "/api/reference/saved":
                APP.set_saved_reference(payload.get("roast_ids"))
                self._json(APP.state())
            elif path == "/api/audio/features":
                self._json(APP.ingest_audio(payload))
            elif path == "/api/feedback":
                self._json(APP.record_feedback(payload))
            elif path == "/api/airflow":
                self._json(APP.set_airflow(payload))
            elif path == "/api/action":
                self._json(APP.confirm_operator_action(str(payload.get("action", ""))))
            else:
                self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            APP.log("API_ERROR", path=self.path, error=str(exc), traceback=traceback.format_exc())
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def main() -> None:
    host, port = "127.0.0.1", int(os.environ.get("ROAST_COMPANION_PORT", "8765"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"Peak Roasting is running offline at {url}", flush=True)
    print("Close this window or press Control-C to stop it.", flush=True)
    if os.environ.get("ROAST_COMPANION_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    def shutdown(*_args: object) -> None:
        APP.close()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        httpd.serve_forever()
    finally:
        APP.close()
        httpd.server_close()


if __name__ == "__main__":
    main()
