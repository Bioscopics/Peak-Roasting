#!/usr/bin/env python3

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from learning_profiles import build_learning_block, with_color_confirmation
from reference_library import ReferenceLibrary
from roast_engine import IR5_DOCUMENTED_MIN_BATCH_G, RoastAnalyzer, batch_guidance, profile_payloads

with patch("threading.Thread.start"):
    import server


ROOT = Path(__file__).resolve().parent
LATEST_ET_DROP_TRACE = (
    (309.37403625, 455.244, 405.991),
    (310.378769084, 452.51, 405.863),
    (311.381979334, 450.208, 405.642),
    (312.387935292, 446.739, 405.28),
    (313.393280667, 442.869, 404.974),
    (314.398764375, 439.157, 404.624),
    (315.404716292, 435.707, 404.427),
    (316.40786925, 432.895, 404.596),
    (317.410994209, 430.596, 404.986),
    (318.412031, 429.028, 405.794),
    (319.416791709, 427.996, 406.528),
    (320.422272209, 427.39, 407.531),
    (321.427812084, 427.229, 408.644),
    (322.433543542, 427.207, 409.774),
    (323.439022584, 427.484, 411.005),
    (324.442609959, 427.854, 412.257),
    (325.444270292, 428.246, 413.13),
    (326.44995375, 428.747, 414.581),
    (327.455650042, 429.294, 415.74),
    (328.461929334, 429.899, 416.754),
    (329.468179876, 430.522, 418.394),
)


@contextmanager
def isolated_companion(roasts=None):
    with tempfile.TemporaryDirectory() as folder:
        data_root = Path(folder) / "data"
        for roast_id, content in roasts or []:
            path = data_root / "roasts" / roast_id / "roast.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content if isinstance(content, str) else json.dumps(content))
        with (
            patch.multiple(
                server,
                DATA_ROOT=data_root,
                ROASTS_ROOT=data_root / "roasts",
                LOGS_ROOT=data_root / "logs",
            ),
            patch.object(server.threading.Thread, "start"),
        ):
            yield server.Companion(), data_root


class ReferenceStop:
    def __init__(self, companion, points):
        self.companion = companion
        self.points = points
        self.index = 0
        self.current = None

    def wait(self, _seconds):
        if self.index >= len(self.points):
            return True
        self.current = self.points[self.index]
        self.index += 1
        self.companion.temperatures = {"et": self.current["et"], "bt": self.current["bt"]}
        return False


class CompanionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = json.loads((ROOT / "reference-roast.json").read_text())

    def test_installed_probe_channel_mapping(self):
        self.assertEqual(server.PHIDGET_CHANNEL_CURVES, {0: "bt", 1: "et"})

    def test_batch_size_defaults_updates_and_clears(self):
        with isolated_companion() as (companion, _data_root):
            self.assertIsNone(companion.meta["batch_g"])
            companion.update_meta({"batch_g": "3250"})
            self.assertEqual(companion.meta["batch_g"], 3250)
            companion.update_meta({"bean": "Keep batch"})
            self.assertEqual(companion.meta["batch_g"], 3250)
            for blank in ("", "   ", None):
                companion.update_meta({"batch_g": blank})
                self.assertIsNone(companion.meta["batch_g"])

    def test_batch_size_invalid_values_are_atomic(self):
        with isolated_companion() as (companion, _data_root):
            companion.update_meta({"bean": "Original", "level": "medium", "batch_g": 2500})
            for invalid in (True, False, 1.5, "1.5", "beans", 0, -1, 5001):
                with self.subTest(invalid=invalid):
                    before = companion.meta.copy()
                    before_log = companion.log_path.read_text()
                    with self.assertRaisesRegex(ValueError, "whole number from 1 to 5000"):
                        companion.update_meta({"bean": "Changed", "level": "dark", "batch_g": invalid})
                    self.assertEqual(companion.meta, before)
                    self.assertEqual(companion.analyzer.profile.key, "medium")
                    self.assertEqual(companion.log_path.read_text(), before_log)

    def test_batch_size_persists_in_saved_roast(self):
        with isolated_companion() as (companion, data_root):
            companion.update_meta({"batch_g": "5000"})
            companion.connected = True
            companion._begin_recording(0)
            companion.analyzer.add_point(10, 350, 300, auto=False)
            roast_id = companion.stop_recording()
            roast = json.loads((data_root / "roasts" / roast_id / "roast.json").read_text())
            self.assertEqual(roast["meta"]["batch_g"], 5000)

    def test_300g_guidance_applies_experimental_warmikuna_temperature_calibration(self):
        guidance = batch_guidance(300, "light")
        self.assertEqual(
            {key: guidance[key] for key in ("batch_g", "mode", "supported_min_g", "time_factor")},
            {"batch_g": 300, "mode": "experimental_underload", "supported_min_g": 453, "time_factor": 1.0},
        )
        self.assertFalse(guidance["time_adjustment_applied"])
        self.assertTrue(guidance["temperature_adjustment_applied"])
        for phrase in ("300 g", "Warmikuna"):
            self.assertIn(phrase, guidance["title"])
        for phrase in ("403°F target", "405°F ceiling", "times remain unchanged"):
            self.assertIn(phrase, guidance["message"])

    def test_effective_profile_is_exactly_300g_light_and_persists_in_state_and_save(self):
        calibrated = RoastAnalyzer("light", 300).profile
        self.assertEqual((calibrated.drop_target_f, calibrated.drop_ceiling_f), (403.0, 405.0))
        self.assertIn("experimental 300 g warmikuna calibration", calibrated.description.lower())
        for batch_g in (299, 301, 453, 5000, None):
            with self.subTest(batch_g=batch_g):
                profile = RoastAnalyzer("light", batch_g).profile
                self.assertEqual((profile.drop_target_f, profile.drop_ceiling_f), (390.0, 392.0))
                self.assertFalse(batch_guidance(batch_g, "light")["temperature_adjustment_applied"])
        self.assertEqual((RoastAnalyzer("medium", 300).profile.drop_target_f, RoastAnalyzer("medium", 300).profile.drop_ceiling_f), (400.0, 405.0))
        self.assertEqual((RoastAnalyzer("correction", 300).profile.drop_target_f, RoastAnalyzer("correction", 300).profile.drop_ceiling_f), (370.0, 375.0))
        for level in ("medium", "correction"):
            with self.subTest(level=level):
                guidance = batch_guidance(300, level)
                self.assertFalse(guidance["temperature_adjustment_applied"])
                self.assertNotIn("Warmikuna", guidance["title"])
                self.assertNotIn("403°F", guidance["message"])

        for payload in ({"level": "light", "batch_g": 300}, {"batch_g": 300, "level": "light"}):
            with self.subTest(order=list(payload)), isolated_companion() as (companion, data_root):
                companion.update_meta(payload)
                self.assertEqual((companion.state()["profile"]["drop_target_f"], companion.state()["profile"]["drop_ceiling_f"]), (403.0, 405.0))
                companion._begin_recording(0)
                companion.analyzer.add_point(10, 410, 390, auto=False)
                roast_id = companion.stop_recording()
                saved = json.loads((data_root / "roasts" / roast_id / "roast.json").read_text())
                self.assertEqual((saved["profile"]["drop_target_f"], saved["profile"]["drop_ceiling_f"]), (403.0, 405.0))

    def test_desired_drop_target_default_custom_clear_prediction_and_atomic_validation(self):
        with isolated_companion() as (companion, _data_root):
            self.assertEqual((companion.meta["desired_drop_f"], companion.analyzer.profile.drop_target_f), (None, 390))
            companion.update_meta({"desired_drop_f": 400})
            for elapsed, bt in ((0, 380), (5, 382.5), (10, 385)):
                companion.analyzer.add_point(elapsed, bt + 20, bt, auto=False)
            prediction = companion.analyzer.drop_prediction()
            self.assertEqual((prediction["target_temp_f"], prediction["seconds_to_drop"]), (400, 30))
            self.assertEqual(companion.analyzer.profile.drop_ceiling_f, 400)
            companion.update_meta({"desired_drop_f": ""})
            self.assertEqual((companion.meta["desired_drop_f"], companion.analyzer.profile.drop_target_f), (None, 390))
            for invalid in (249, 501, True, "bad", float("nan")):
                before = (companion.meta.copy(), companion.analyzer.profile)
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    companion.update_meta({"bean": "Changed", "desired_drop_f": invalid})
                self.assertEqual((companion.meta, companion.analyzer.profile), before)
            companion.update_meta({"level": "correction"})
            with self.assertRaisesRegex(ValueError, "fixed drop target"):
                companion.update_meta({"desired_drop_f": 371})

    def test_drop_prediction_is_consistent_at_below_at_and_above_target(self):
        for current_bt, expected_seconds in ((385, 30), (400, 0), (447, 0)):
            with self.subTest(current_bt=current_bt):
                analyzer = RoastAnalyzer("light", desired_drop_f=400)
                for elapsed, bt in ((0, current_bt - 5), (5, current_bt - 2.5), (10, current_bt)):
                    analyzer.add_point(elapsed, bt + 20, bt, auto=False)
                prediction = analyzer.drop_prediction()
                self.assertEqual(prediction["target_temp_f"], 400)
                self.assertEqual(prediction["seconds_to_drop"], expected_seconds)

    def test_drop_result_persists_blank_miss_and_corrected_temperature_without_changing_points(self):
        with isolated_companion() as (companion, data_root):
            companion.update_meta({"desired_drop_f": 391})
            companion._begin_recording(0)
            companion.analyzer.add_point(10, 410, 392, auto=False)
            roast_id = companion.stop_recording()
            path = data_root / "roasts" / roast_id / "roast.json"
            expected = {
                "target_f": 391.0, "sensor_drop_f": 392.0, "recorded_drop_f": 392.0,
                "delta_f": 1.0, "within_1f": True, "missed_target": False,
                "actual_override_f": None, "status": "on_target",
            }
            self.assertEqual(companion.state()["drop_result"], expected)
            self.assertEqual(json.loads(path.read_text())["drop_result"], expected)
            self.assertEqual(json.loads(path.read_text())["meta"]["desired_drop_f"], 391)
            original_points = companion.analyzer.points.copy()
            original_drop = companion.analyzer.events["DROP"].copy()
            companion.record_drop_result({"missed": True})
            self.assertEqual(companion.state()["drop_result"]["status"], "missed_target")
            self.assertEqual(companion.analyzer.events["DROP"], original_drop)
            before = (companion.analyzer.events["DROP"].copy(), path.read_bytes())
            for payload in ({"missed": False}, {"missed": True, "actual_drop_f": 249}, {"missed": True, "actual_drop_f": 501}):
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    companion.record_drop_result(payload)
                self.assertEqual((companion.analyzer.events["DROP"], path.read_bytes()), before)
            state = companion.record_drop_result({"missed": True, "actual_drop_f": 390.5})
            result = state["drop_result"]
            self.assertEqual((result["sensor_drop_f"], result["recorded_drop_f"], result["actual_override_f"]), (392, 390.5, 390.5))
            self.assertEqual((result["within_1f"], result["missed_target"], result["status"]), (True, True, "missed_target"))
            self.assertEqual(companion.analyzer.events["DROP"]["source"], "manual corrected drop temperature")
            self.assertEqual(companion.analyzer.points, original_points)
            for active in ("recording", "replaying", "recording_armed"):
                setattr(companion, active, True)
                with self.subTest(active=active), self.assertRaisesRegex(ValueError, "completed current saved"):
                    companion.record_drop_result({"missed": True})
                setattr(companion, active, False)
            companion.new_roast()
            self.assertEqual(companion.view_history(roast_id)["drop_result"], result)
            with self.assertRaisesRegex(ValueError, "completed current saved"):
                companion.record_drop_result({"missed": True})
            companion.viewing_history = False
            companion.analyzer.set_profile("correction")
            with self.assertRaisesRegex(ValueError, "read-only"):
                companion.record_drop_result({"missed": True})

    def test_batch_guidance_boundary_and_unset_modes(self):
        self.assertEqual(IR5_DOCUMENTED_MIN_BATCH_G, 453)
        for batch_g, mode in ((453, "documented_range"), (None, "unset")):
            with self.subTest(batch_g=batch_g):
                guidance = batch_guidance(batch_g, "light")
                self.assertEqual((guidance["mode"], guidance["time_factor"]), (mode, 1.0))
                self.assertFalse(guidance["time_adjustment_applied"])
                self.assertFalse(guidance["temperature_adjustment_applied"])

    def test_batch_update_does_not_change_profiles(self):
        before = profile_payloads()
        with isolated_companion() as (companion, _data_root):
            companion.update_meta({"batch_g": 300})
            self.assertEqual(profile_payloads(), before)
            self.assertEqual(companion.state()["profiles"], before)

    def test_history_loads_newest_unique_origin_and_bean_values_then_seeds(self):
        roasts = [
            ("20260101_000000", {"saved_at": "2026-01-01", "meta": {"origin": " Older ", "bean": " Typica "}}),
            ("20260103_000000", {"saved_at": "2026-01-03", "meta": {"origin": " Kenya ", "bean": " Bourbon "}}),
            ("20260102_000000", {"saved_at": "2026-01-02", "meta": {"origin": "kenya", "bean": "bourbon"}}),
            ("20260105_000000", {"meta": {"origin": "   "}}),
            ("20260104_000000", "{malformed"),
        ]
        with isolated_companion(roasts) as (companion, _data_root):
            self.assertEqual(companion.origin_suggestions[:4], ["Kenya", "Older", "Brazil", "Colombia"])
            self.assertEqual(companion.bean_suggestions[:4], ["Bourbon", "Typica", "Caturra", "Catuai"])
            self.assertEqual(companion.origin_suggestions[-1], "Burundi")
            self.assertEqual(companion.bean_suggestions[-1], "Ruiru 11")
            history = companion.state()["roast_history"]
            self.assertEqual([item["id"] for item in history], ["20260105_000000", "20260103_000000", "20260102_000000", "20260101_000000"])
            self.assertEqual(
                history[1],
                {"id": "20260103_000000", "label": "Kenya · Bourbon", "bean": "Bourbon", "origin": "Kenya", "level": "", "batch_g": None, "saved_at": "2026-01-03"},
            )

    def test_history_suggestions_canonicalize_seeds_and_unknown_labels(self):
        roasts = [
            ("20260101_000000", {"meta": {"origin": " brazil ", "bean": " SANTOS "}}),
            ("20260103_000000", {"meta": {"origin": "  indonesia ", "bean": "  santos  "}}),
        ]
        with isolated_companion(roasts) as (companion, _data_root):
            self.assertEqual(companion.origin_suggestions[:2], ["Indonesia", "Brazil"])
            self.assertEqual(companion.bean_suggestions[:2], ["Santos", "Bourbon"])

    def test_metadata_labels_collapse_whitespace_title_case_and_canonicalize_acronyms(self):
        with isolated_companion() as (companion, _data_root):
            companion.update_meta({
                "bean": "  nyamasheke   lot 12 ",
                "origin": "  south   kivu ",
                "notes": "  preserve   Notes  ",
            })
            self.assertEqual(companion.meta, {
                "bean": "Nyamasheke Lot 12", "origin": "South Kivu", "level": "light",
                "notes": "  preserve   Notes  ", "batch_g": None, "desired_drop_f": None,
                "teach_mode": False, "roast_level_label": "", "learned_profile_id": None,
            })
            companion.update_meta({"bean": " sl28 ", "origin": " rWaNdA "})
            self.assertEqual((companion.meta["bean"], companion.meta["origin"]), ("SL28", "Rwanda"))
            self.assertEqual((companion.bean_suggestions[0], companion.origin_suggestions[0]), ("SL28", "Rwanda"))

    def test_canonical_metadata_persists_in_saved_roast(self):
        with isolated_companion() as (companion, data_root):
            companion.update_meta({"bean": " yellow   bourbon ", "origin": " el   salvador "})
            companion._begin_recording(0)
            companion.analyzer.add_point(10, 350, 300, auto=False)
            roast_id = companion.stop_recording()
            roast = json.loads((data_root / "roasts" / roast_id / "roast.json").read_text())
            self.assertEqual(roast["meta"]["bean"], "Yellow Bourbon")
            self.assertEqual(roast["meta"]["origin"], "El Salvador")

    def test_new_origin_and_batch_guidance_serialize_in_state(self):
        with isolated_companion() as (companion, _data_root):
            companion.update_meta({"origin": "  Colombia  ", "bean": "  Castillo  ", "batch_g": 300})
            state = companion.state()
            self.assertEqual(state["origin_suggestions"][0], "Colombia")
            self.assertEqual(state["bean_suggestions"][0], "Castillo")
            self.assertEqual(state["batch_guidance"], batch_guidance(300, "light"))
            json.dumps(state)

    def test_history_view_rejects_non_child_and_active_session_ids(self):
        roast = {"meta": {}, "points": [], "events": []}
        with isolated_companion([("safe", roast)]) as (companion, data_root):
            outside = data_root / "outside" / "roast.json"
            outside.parent.mkdir(parents=True)
            outside.write_text(json.dumps(roast))
            for roast_id in ("../outside", "missing", "safe/../safe"):
                with self.subTest(roast_id=roast_id), self.assertRaisesRegex(ValueError, "Invalid roast history id"):
                    companion.view_history(roast_id)
            companion.connected = True
            companion.start_recording()
            with self.assertRaisesRegex(ValueError, "armed capture"):
                companion.view_history("safe")

    def test_history_view_restores_completed_graph_without_rewriting_saved_file(self):
        guidance = {
            "sensor": "bt", "target_f": 415.0, "low_f": 350.0, "high_f": 440.0,
            "basis": "Saved guidance", "experimental_underload": False, "batch_adjustment_applied": False,
        }
        roast = {
            "id": "20260106_000000",
            "saved_at": "2026-01-06T00:00:00-05:00",
            "meta": {"bean": "Gesha", "origin": "Panama", "level": "medium", "notes": "Saved", "batch_g": 500},
            "points": [{"elapsed": 0, "et": 420, "bt": 410}, {"elapsed": 12, "et": 405, "bt": 380}],
            "events": [
                {"name": "CHARGE", "elapsed": 0, "et": 420, "bt": 410, "confidence": 1, "source": "manual"},
                {"name": "DROP", "elapsed": 12, "et": 405, "bt": 380, "confidence": 1, "source": "manual"},
            ],
            "precharge_points": [{"elapsed": 0, "et": 421, "bt": 411}, {"elapsed": 2, "et": 420, "bt": 410}],
            "charge_guidance": guidance,
        }
        with isolated_companion([("20260106_000000", roast)]) as (companion, data_root):
            saved = data_root / "roasts" / "20260106_000000" / "roast.json"
            before = saved.read_bytes()
            state = companion.view_history("20260106_000000")
            self.assertEqual(saved.read_bytes(), before)
            self.assertEqual(state["meta"], {
                **roast["meta"], "desired_drop_f": None, "teach_mode": False,
                "roast_level_label": "", "learned_profile_id": None,
            })
            self.assertEqual(state["points"], roast["points"])
            self.assertEqual([event["name"] for event in state["events"]], ["CHARGE", "DROP"])
            self.assertEqual(state["precharge_points"], roast["precharge_points"])
            self.assertEqual(state["charge_guidance"], guidance)
            self.assertEqual((state["recording"], state["replaying"], state["auto_start_armed"], state["last_saved"]), (False, False, False, "20260106_000000"))
            self.assertEqual(json.loads(companion.log_path.read_text().splitlines()[-1])["event"], "HISTORY_VIEW")
            with self.assertRaisesRegex(ValueError, "read-only"):
                companion.update_meta({"bean": "Changed"})
            self.assertEqual(saved.read_bytes(), before)

    def test_armed_noise_and_gradual_cooldown_do_not_auto_start(self):
        cases = {
            "noise": [300, 302, 299, 301, 298] * 8,
            "gradual": list(range(300, 287, -1)) + [288] * 27,
        }
        for name, temperatures in cases.items():
            with self.subTest(name=name), isolated_companion() as (companion, _data_root):
                companion.connected = True
                companion.start_recording()
                points = [{"elapsed": index, "et": 400, "bt": bt} for index, bt in enumerate(temperatures)]
                companion.stop = ReferenceStop(companion, points)
                with patch.object(server.time, "monotonic", side_effect=lambda: companion.stop.current["elapsed"]):
                    companion._sampler()
                self.assertFalse(companion.recording)
                self.assertTrue(companion.state()["auto_start_armed"])
                self.assertNotIn("CHARGE", companion.analyzer.events)
                self.assertEqual(len(companion.precharge_capture), len(temperatures))

    def test_auto_charge_backdates_timer_and_turning_point_stays_automatic(self):
        with isolated_companion() as (companion, _data_root):
            companion.connected = True
            idle_bt = [400, 402, 404, 406, 408, 410, 415, 413, 411, 409, 407]
            points = [{"elapsed": 100 + index, "et": 400, "bt": bt} for index, bt in enumerate(idle_bt)]
            points += [
                {"elapsed": 116, "et": 390, "bt": 270},
                {"elapsed": 126, "et": 380, "bt": 250},
                {"elapsed": 136, "et": 375, "bt": 240},
                {"elapsed": 142, "et": 380, "bt": 245},
                {"elapsed": 146, "et": 390, "bt": 255},
            ]
            companion.stop = ReferenceStop(companion, points)
            with patch.object(server.time, "monotonic", side_effect=lambda: companion.stop.current["elapsed"]):
                companion._sampler()
            self.assertFalse(companion.recording)
            self.assertFalse(companion.state()["auto_start_armed"])
            self.assertNotIn("CHARGE", companion.analyzer.events)

            companion.temperatures = {"et": None, "bt": None}
            companion.start_recording()
            armed = companion.state()
            self.assertEqual((armed["auto_start_armed"], armed["recording"], armed["elapsed"]), (True, False, 0))
            self.assertIsNone(companion.started_monotonic)
            self.assertEqual((companion.analyzer.points, companion.analyzer.events), ([], {}))
            companion.start_recording()
            self.assertTrue(companion.state()["auto_start_armed"])
            self.assertIsNone(companion.started_monotonic)
            companion.stop = ReferenceStop(companion, points)
            with patch.object(server.time, "monotonic", side_effect=lambda: companion.stop.current["elapsed"]):
                companion._sampler()
            charge = companion.analyzer.events["CHARGE"]
            self.assertTrue(companion.recording)
            self.assertFalse(companion.state()["auto_start_armed"])
            self.assertEqual(companion.meta["bean"], "")
            self.assertEqual(companion.started_monotonic, 106)
            self.assertEqual((charge["elapsed"], charge["source"]), (0, "automatic BT drop"))
            self.assertEqual(companion.analyzer.points[0]["elapsed"], 0)
            self.assertEqual(companion.analyzer.events["TP"]["bt"], 240)
            with patch.object(server.time, "monotonic", return_value=146):
                self.assertEqual(companion.state()["elapsed"], 40)
            logs = [json.loads(line) for line in companion.log_path.read_text().splitlines()]
            self.assertEqual(sum(item["event"] == "RECORD_ARM" for item in logs), 1)
            self.assertEqual(sum(item["event"] == "AUTO_RECORD_START" for item in logs), 1)

    def test_auto_charge_rejects_spike_without_rising_baseline_and_interrupted_decline(self):
        cases = {
            "single spike and drop": (300, 300, 300, 300, 300, 350, 335, 320, 305, 290),
            "interrupted decline": (300, 302, 304, 306, 308, 310, 308, 306, 307, 304, 301),
        }
        for name, temperatures in cases.items():
            with self.subTest(name=name), isolated_companion() as (companion, _data_root):
                companion.connected = True
                companion.start_recording()
                points = [
                    {"elapsed": index, "et": 400, "bt": bt}
                    for index, bt in enumerate(temperatures)
                ]
                companion.stop = ReferenceStop(companion, points)
                with patch.object(server.time, "monotonic", side_effect=lambda: companion.stop.current["elapsed"]):
                    companion._sampler()
                self.assertFalse(companion.recording)
                self.assertNotIn("CHARGE", companion.analyzer.events)

    def test_saved_inverse_turning_point_shape_auto_starts_at_peak(self):
        with isolated_companion() as (companion, _data_root):
            companion.connected = True
            companion.start_recording()
            points = [
                {"elapsed": elapsed, "et": et, "bt": bt}
                for elapsed, et, bt in (
                    (21.81, 375.211, 413.173),
                    (22.82, 375.705, 413.804),
                    (23.82, 376.325, 414.331),
                    (24.82, 376.836, 414.778),
                    (25.83, 377.41, 415.279),
                    (26.83, 377.901, 415.503),
                    (27.84, 378.467, 415.253),
                    (28.84, 378.935, 414.29),
                    (29.85, 379.48, 412.868),
                    (30.85, 379.799, 410.879),
                    (31.85, 380.284, 408.435),
                    (32.86, 380.794, 405.923),
                    (33.86, 381.256, 402.999),
                    (34.86, 381.72, 400.738),
                )
            ]
            companion.stop = ReferenceStop(companion, points)
            with patch.object(server.time, "monotonic", side_effect=lambda: companion.stop.current["elapsed"]):
                companion._sampler()
            self.assertTrue(companion.recording)
            self.assertEqual(companion.started_monotonic, 26.83)
            self.assertEqual(companion.analyzer.events["CHARGE"]["bt"], 415.5)
            self.assertEqual(companion.analyzer.points[-1]["bt"], 400.738)

    def test_exact_20260912_184233_trace_auto_starts_and_backdates_to_peak(self):
        saved = json.loads((ROOT / "data" / "roasts" / "20260912_184233" / "roast.json").read_text())
        precharge = saved["precharge_points"]
        self.assertEqual(max(point["bt"] for point in precharge), 429.236)
        with isolated_companion() as (companion, _data_root):
            companion.connected = True
            companion.start_recording()
            charge = None
            for point in precharge:
                charge = companion._process_charge_sample(point["elapsed"], point["et"], point["bt"])
                if charge:
                    break
            self.assertTrue(companion.recording)
            self.assertFalse(companion.recording_armed)
            self.assertEqual(companion.started_monotonic, 4.02)
            self.assertEqual((charge["elapsed"], charge["bt"], charge["source"]), (0, 429.24, "automatic BT drop"))

    def test_back_to_back_slow_cooling_baseline_auto_starts_at_accelerated_fall(self):
        with isolated_companion() as (companion, _data_root):
            companion.connected = True
            companion.start_recording()
            points = [
                {"elapsed": elapsed, "et": et, "bt": bt}
                for elapsed, et, bt in (
                    (4.77, 424.246, 465.903), (5.77, 423.902, 465.754),
                    (6.77, 423.559, 465.741), (7.78, 423.281, 465.265),
                    (8.78, 422.996, 464.317), (9.78, 422.683, 462.510),
                    (10.79, 422.513, 460.340), (11.79, 422.429, 457.761),
                    (12.80, 422.524, 454.562), (13.80, 422.568, 452.125),
                )
            ]
            companion.stop = ReferenceStop(companion, points)
            with patch.object(server.time, "monotonic", side_effect=lambda: companion.stop.current["elapsed"]):
                companion._sampler()
            self.assertTrue(companion.recording)
            self.assertEqual(companion.started_monotonic, 9.78)
            self.assertEqual(companion.analyzer.events["CHARGE"]["bt"], 462.51)
            self.assertEqual(companion.analyzer.points[-1]["bt"], 452.125)

    def test_arm_exposes_capture_without_roast_time_and_manual_charge_is_immediate(self):
        with isolated_companion() as (companion, _data_root):
            companion.connected = True
            companion.temperatures = {"et": 420, "bt": 410}
            with patch.object(server.time, "monotonic", side_effect=(100, 103, 103)):
                companion.start_recording()
                armed = companion.state()
                charge = companion.mark("CHARGE")
            self.assertEqual((armed["recording"], armed["elapsed"], armed["capture_elapsed"]), (False, 0, 3))
            self.assertEqual(armed["precharge_points"], [{"elapsed": 0, "et": 420, "bt": 410}])
            self.assertEqual((charge["elapsed"], charge["source"]), (0, "manual beans in"))
            self.assertTrue(companion.recording)
            self.assertEqual(companion.analyzer.points, [{"elapsed": 0, "et": 420, "bt": 410}])
            logs = [json.loads(line) for line in companion.log_path.read_text().splitlines()]
            self.assertEqual(sum(item["event"] == "MANUAL_RECORD_START" for item in logs), 1)

    def test_correction_profile_payload(self):
        correction = profile_payloads()["correction"]
        expected = {
            "mode": "correction",
            "charge_et_low_f": 285.0,
            "charge_et_high_f": 300.0,
            "target_ror_low": 8.0,
            "target_ror_high": 12.0,
            "sample_start_seconds": 180,
            "dump_window_seconds": 210,
            "drop_target_f": 370.0,
            "drop_ceiling_f": 375.0,
            "absolute_ceiling_f": 380.0,
            "max_seconds": 300,
            "manual_dump": True,
        }
        self.assertEqual({key: correction[key] for key in expected}, expected)

    def test_correction_suppresses_normal_inference_and_preserves_manual_drop(self):
        analyzer = RoastAnalyzer("correction")
        for point in self.reference["points"]:
            analyzer.add_point(point["elapsed"], point["et"], point["bt"])
        analyzer.backfill()
        self.assertTrue(set(analyzer.events) <= {"CHARGE", "TP", "DROP"})
        self.assertEqual(analyzer.events["DROP"]["source"], "automatic physical dump response")
        self.assertIsNone(analyzer.drop_prediction())

        point = self.reference["points"][-1]
        analyzer.mark_event("DROP", **point, confidence=1, source="manual")
        analyzer.backfill()
        self.assertEqual(analyzer.events["DROP"]["source"], "manual")
        self.assertTrue(set(analyzer.events) <= {"CHARGE", "TP", "DROP"})

    def test_correction_alerts_are_charge_relative_and_one_shot(self):
        analyzer = RoastAnalyzer("correction")
        analyzer.mark_event("CHARGE", elapsed=50, et=300, bt=350, source="manual")
        analyzer.mark_event("TP", elapsed=60, et=290, bt=340, source="manual")
        for elapsed, bt in ((229, 374), (230, 374), (320, 374), (345, 374), (346, 375), (347, 380), (400, 390)):
            analyzer.add_point(elapsed, bt + 15, bt)

        self.assertEqual(len(analyzer.alerts), 5)
        self.assertEqual([alert["elapsed"] for alert in analyzer.alerts], [230, 320, 345, 346, 347])
        self.assertEqual(
            [alert["kind"] for alert in analyzer.alerts],
            ["stage", "warning", "dump_warning", "ceiling_warning", "dump_warning"],
        )
        self.assertIn("sample now", analyzer.alerts[0]["message"])
        self.assertIn("HARD LIMIT", analyzer.alerts[-1]["message"])

    def test_reference_curve_detects_physical_dump_spike(self):
        analyzer = RoastAnalyzer("light")
        for point in self.reference["points"]:
            analyzer.add_point(point["elapsed"], point["et"], point["bt"])
            if "DROP" in analyzer.events:
                break
        events = analyzer.events
        self.assertIn("CHARGE", events)
        self.assertIn("FCs", events)
        self.assertIn("DROP", events)
        self.assertNotIn("SCs", events)
        self.assertEqual(events["DROP"]["elapsed"], 491.9)
        self.assertEqual(events["DROP"]["source"], "automatic physical dump response")

    def test_ceiling_warning_requires_charge_and_turning_point(self):
        analyzer = RoastAnalyzer("light", 300)
        analyzer.add_point(0, 420, 415)
        self.assertEqual([alert for alert in analyzer.alerts if alert["kind"] == "ceiling_warning"], [])
        analyzer.mark_event("CHARGE", elapsed=0, et=420, bt=415, source="test")
        analyzer.add_point(1, 418, 410)
        self.assertEqual([alert for alert in analyzer.alerts if alert["kind"] == "ceiling_warning"], [])
        analyzer.mark_event("TP", elapsed=1, et=418, bt=410, source="test")
        analyzer.add_point(2, 415, 403)
        warnings = [alert for alert in analyzer.alerts if alert["kind"] == "ceiling_warning"]
        self.assertEqual(
            [(alert["elapsed"], alert["message"]) for alert in warnings],
            [(2.0, "DROP TARGET REACHED — TARGET 403°F · 2.0°F TO 405°F CEILING")],
        )

    def test_et_collapse_requires_later_confirmation_and_backdates_to_onset(self):
        analyzer = RoastAnalyzer("light", 300)
        analyzer.mark_event("CHARGE", elapsed=0, et=380, bt=415, source="test")
        for point in LATEST_ET_DROP_TRACE[:4]:
            analyzer.add_point(*point)
        self.assertNotIn("DROP", analyzer.events)
        for point in LATEST_ET_DROP_TRACE[4:]:
            analyzer.add_point(*point)
            if "DROP" in analyzer.events:
                break
        drop = analyzer.events["DROP"]
        self.assertEqual((drop["elapsed"], drop["source"]), (309.37, "automatic physical dump response"))
        self.assertEqual(analyzer.points[-1]["elapsed"], 329.468179876)

    def test_routine_pilot_cooling_et_decline_does_not_mark_or_finalize(self):
        points = tuple(
            (309 + offset, et, bt)
            for offset, (et, bt) in enumerate(((455, 404), (452, 403.9), (449, 403.8), (446, 403.7)))
        )
        analyzer = RoastAnalyzer("light", 300)
        analyzer.mark_event("CHARGE", elapsed=0, et=380, bt=415, source="test")
        for point in points:
            analyzer.add_point(*point)
        self.assertNotIn("DROP", analyzer.events)

        with isolated_companion() as (companion, _data_root):
            companion.update_meta({"level": "light", "batch_g": 300})
            companion._begin_recording(0)
            companion.analyzer.mark_event("CHARGE", elapsed=0, et=380, bt=415, source="test")
            companion.stop = ReferenceStop(
                companion, [{"elapsed": elapsed, "et": et, "bt": bt} for elapsed, et, bt in points]
            )
            with (
                patch.object(companion, "_save", wraps=companion._save) as save,
                patch.object(server.time, "monotonic", side_effect=lambda: companion.stop.current["elapsed"]),
            ):
                companion._sampler()
            self.assertTrue(companion.recording)
            self.assertIsNone(companion.last_saved)
            self.assertNotIn("DROP", companion.analyzer.events)
            self.assertEqual(save.call_count, 0)

    def test_et_candidate_rejects_missing_confirmation_predicates_and_expiry(self):
        base_elapsed = LATEST_ET_DROP_TRACE[0][0]
        cases = {
            "no rebound": [(elapsed, et, min(bt, 405.28)) for elapsed, et, bt in LATEST_ET_DROP_TRACE],
            "no gap convergence": [
                point if index < 4 else (point[0], point[1] + 3, point[2])
                for index, point in enumerate(LATEST_ET_DROP_TRACE)
            ],
            "under 20F fall": [
                point if index < 4 else (
                    point[0],
                    max(point[1], 436),
                    point[2] + (422 - LATEST_ET_DROP_TRACE[-1][2]) * (index - 3) / (len(LATEST_ET_DROP_TRACE) - 4),
                )
                for index, point in enumerate(LATEST_ET_DROP_TRACE)
            ],
            "low RoR": [
                (base_elapsed + (elapsed - base_elapsed) * 1.5, et, bt)
                for elapsed, et, bt in LATEST_ET_DROP_TRACE[:-1]
            ],
            "expired": [
                (base_elapsed + (elapsed - base_elapsed) * 1.6, et, bt)
                for elapsed, et, bt in LATEST_ET_DROP_TRACE
            ],
        }
        for name, points in cases.items():
            with self.subTest(name=name):
                analyzer = RoastAnalyzer("light", 300)
                analyzer.mark_event("CHARGE", elapsed=0, et=380, bt=415, source="test")
                for point in points:
                    analyzer.add_point(*point)
                self.assertNotIn("DROP", analyzer.events)

    def test_et_candidate_accepts_exact_90_second_and_target_boundaries(self):
        first_elapsed = LATEST_ET_DROP_TRACE[0][0]
        minimum_bt = min(point[2] for point in LATEST_ET_DROP_TRACE)
        points = [
            (
                90 + elapsed - first_elapsed,
                et,
                403.0 if index < 4 else 403.0 + bt - minimum_bt,
            )
            for index, (elapsed, et, bt) in enumerate(LATEST_ET_DROP_TRACE)
        ]
        analyzer = RoastAnalyzer("light", 300)
        analyzer.mark_event("CHARGE", elapsed=0, et=380, bt=415, source="test")
        for point in points[:4]:
            analyzer.add_point(*point)
        self.assertNotIn("DROP", analyzer.events)
        for point in points[4:]:
            analyzer.add_point(*point)
            if "DROP" in analyzer.events:
                break
        self.assertEqual((analyzer.events["DROP"]["elapsed"], analyzer.events["DROP"]["bt"]), (90.0, 403.0))

    def test_et_collapse_rejects_rising_bt_interruption_and_sub_eight_total(self):
        cases = {
            "rising BT": ((455, 404), (452, 405), (449, 406), (446, 407)),
            "interrupted ET": ((455, 405.9), (452, 405.8), (451, 405.7), (447, 405.6)),
            "under eight total": ((455, 405.9), (452.5, 405.8), (450, 405.7), (447.5, 405.6)),
        }
        for name, values in cases.items():
            with self.subTest(name=name):
                analyzer = RoastAnalyzer("light", 300)
                analyzer.mark_event("CHARGE", elapsed=0, et=380, bt=415, source="test")
                for offset, (et, bt) in enumerate(values):
                    analyzer.add_point(309 + offset, et, bt)
                self.assertNotIn("DROP", analyzer.events)

    def test_physical_dump_finalizes_and_saves_once_in_normal_and_correction_modes(self):
        for profile in ("light", "correction"):
            with self.subTest(profile=profile), isolated_companion() as (companion, data_root):
                companion.connected = True
                companion.update_meta({"level": profile})
                companion._begin_recording(0)
                companion.stop = ReferenceStop(companion, self.reference["points"])
                with (
                    patch.object(companion, "_save", wraps=companion._save) as save,
                    patch.object(server.time, "monotonic", side_effect=lambda: companion.stop.current["elapsed"]),
                ):
                    companion._sampler()
                    saved_id = companion.stop_recording()

                roast = json.loads((data_root / "roasts" / saved_id / "roast.json").read_text())
                drop = next(event for event in roast["events"] if event["name"] == "DROP")
                logs = [json.loads(line) for line in companion.log_path.read_text().splitlines()]
                self.assertFalse(companion.recording)
                self.assertFalse(companion.recording_armed)
                self.assertFalse(companion.state()["auto_start_armed"])
                self.assertEqual(save.call_count, 1)
                self.assertEqual(drop["source"], "automatic physical dump response")
                self.assertEqual(drop["elapsed"], 491.9)
                self.assertEqual(roast["profile"]["key"], profile)
                self.assertEqual(sum(item["event"] == "AUTO_DROP_FINALIZE" for item in logs), 1)

    def test_et_confirmed_dump_finalizes_and_saves_once(self):
        with isolated_companion() as (companion, data_root):
            companion.connected = True
            companion.update_meta({"level": "light", "batch_g": 300})
            companion._begin_recording(0)
            companion.analyzer.mark_event("CHARGE", elapsed=0, et=380, bt=415, source="test")
            companion.stop = ReferenceStop(
                companion,
                [{"elapsed": elapsed, "et": et, "bt": bt} for elapsed, et, bt in LATEST_ET_DROP_TRACE],
            )
            with (
                patch.object(companion, "_save", wraps=companion._save) as save,
                patch.object(server.time, "monotonic", side_effect=lambda: companion.stop.current["elapsed"]),
            ):
                companion._sampler()

            roast = json.loads((data_root / "roasts" / companion.last_saved / "roast.json").read_text())
            drop = next(event for event in roast["events"] if event["name"] == "DROP")
            logs = [json.loads(line) for line in companion.log_path.read_text().splitlines()]
            self.assertFalse(companion.recording)
            self.assertEqual(save.call_count, 1)
            self.assertEqual((drop["elapsed"], drop["source"]), (309.37, "automatic physical dump response"))
            self.assertEqual(sum(item["event"] == "AUTO_DROP_FINALIZE" for item in logs), 1)

    def test_physical_dump_detection_is_independent_of_desired_target(self):
        analyzer = RoastAnalyzer("light", 300, desired_drop_f=450)
        analyzer.mark_event("CHARGE", elapsed=0, et=380, bt=415, source="test")
        for point in LATEST_ET_DROP_TRACE:
            analyzer.add_point(*point)
            if "DROP" in analyzer.events:
                break
        self.assertEqual(analyzer.profile.drop_target_f, 450)
        self.assertEqual(analyzer.events["DROP"]["source"], "automatic physical dump response")

    def test_correction_state_passes_charge_relative_guide_values(self):
        with isolated_companion() as (companion, _data_root):
            companion.update_meta({"level": "correction"})
            companion.analyzer.add_point(100, 350, 340, auto=False)
            companion.analyzer.mark_event("CHARGE", elapsed=100, et=350, bt=340, source="manual")
            companion.analyzer.add_point(280, 380, 365, auto=False)
            with patch.object(
                server, "next_instruction", return_value={"id": "test", "fuel_guidance": {"band": "mid"}}
            ) as guide:
                state = companion.state()
            self.assertEqual(guide.call_args.kwargs["profile_key"], "correction")
            self.assertEqual(guide.call_args.kwargs["elapsed"], 180)
            self.assertEqual(guide.call_args.kwargs["ror"], companion.analyzer.ror)
            self.assertEqual(state["airflow"]["recommended"]["position"], "drum")
            self.assertEqual(state["timeline"]["next_step_id"], "tp")
            self.assertEqual(set(state["timeline"]), {"steps", "next_step_id", "next_summary"})

            companion.analyzer.events.pop("CHARGE")
            with patch.object(
                server, "next_instruction", return_value={"id": "fallback", "fuel_guidance": {"band": "high"}}
            ) as guide:
                companion.state()
            self.assertEqual(guide.call_args.kwargs["elapsed"], 280)

    def test_correction_airflow_boundaries_and_temperature_override(self):
        with isolated_companion() as (companion, _data_root):
            companion.update_meta({"level": "correction"})
            recommendations = [companion.recommended_airflow(elapsed) for elapsed in (179.9, 180, 269.9, 270)]
            self.assertEqual([item["position"] for item in recommendations], ["half", "drum", "drum", "cooling"])
            self.assertTrue(all("Correction" in item["reason"] for item in recommendations))
            companion.analyzer.add_point(10, 385, 370, auto=False)
            self.assertEqual(companion.recommended_airflow(10)["position"], "cooling")
            companion.analyzer.points[-1]["bt"] = 350
            companion.analyzer.mark_event("DROP", elapsed=10, et=385, bt=350, source="manual")
            self.assertEqual(companion.recommended_airflow(10)["position"], "cooling")

    def test_correction_audio_logs_without_crack_inference(self):
        with isolated_companion() as (companion, _data_root):
            companion.update_meta({"level": "correction"})
            companion.recording = True
            companion.analyzer.mark_event("CHARGE", elapsed=0, et=350, bt=340, source="manual")
            companion.analyzer.add_point(400, 410, 395, auto=False)
            with patch.object(companion.references, "fc_likelihood", return_value={"probability": 1.0}):
                feature = companion.ingest_audio({"elapsed": 400, "rms": 0.2, "baseline": 0.01, "pops_8s": 8, "sharpness": 0.9})
            self.assertEqual(feature, companion.audio_features[-1])
            self.assertNotIn("FCs", companion.analyzer.events)

            companion.analyzer.mark_event("FCs", elapsed=300, et=390, bt=370, source="manual")
            companion.ingest_audio({"elapsed": 400, "rms": 0.2, "baseline": 0.01, "pops_8s": 8, "sharpness": 0.9})
            logs = [json.loads(line) for line in companion.log_path.read_text().splitlines()]
            self.assertNotIn("SCs", companion.analyzer.events)
            self.assertEqual(sum(item["event"] == "AUDIO_FEATURE" for item in logs), 2)

    def test_manual_stop_uses_same_idempotent_finalizer(self):
        with isolated_companion() as (companion, data_root):
            companion.connected = True
            companion._begin_recording(0)
            companion.analyzer.add_point(10, 350, 300, auto=False)
            with patch.object(companion, "_save", wraps=companion._save) as save:
                first_id = companion.stop_recording()
                second_id = companion.stop_recording()
            logs = [json.loads(line) for line in companion.log_path.read_text().splitlines()]
            self.assertEqual(first_id, second_id)
            self.assertEqual(save.call_count, 1)
            self.assertTrue((data_root / "roasts" / first_id / "roast.json").is_file())
            self.assertEqual(companion.analyzer.events["DROP"]["source"], "Stop / Dump button")
            self.assertEqual(sum(item["event"] == "RECORD_STOP" for item in logs), 1)

    def test_manual_first_crack_only_marks_history_and_log(self):
        with isolated_companion() as (companion, _data_root):
            companion._begin_recording(0)
            companion.analyzer.add_point(10, 410, 365, auto=False)
            before_alerts = companion.analyzer.alerts.copy()
            before_actions = json.loads(json.dumps(companion.operator_actions))
            before_airflow = json.loads(json.dumps(companion.airflow))
            event = companion.mark("FCs")
            self.assertEqual((event["name"], event["source"]), ("FCs", "manual"))
            self.assertEqual(companion.analyzer.alerts, before_alerts)
            self.assertEqual(companion.operator_actions, before_actions)
            self.assertEqual(companion.airflow, before_airflow)
            self.assertEqual(companion.manual_history, [("FCs", None)])
            self.assertEqual(json.loads(companion.log_path.read_text().splitlines()[-1])["event"], "EVENT_MARK")

    def test_new_roast_resets_session_and_preserves_machine_and_saved_data(self):
        with isolated_companion() as (companion, data_root):
            companion.connected = True
            companion.temperatures = {"et": 410, "bt": 300}
            sensor = object()
            companion.sensors = [sensor]
            companion.update_meta({"bean": "Preserved", "batch_g": 3250})
            companion._begin_recording(0)
            companion.analyzer.add_point(10, 350, 300, auto=False)
            roast_id = companion.stop_recording()
            saved = data_root / "roasts" / roast_id / "roast.json"
            companion.audio_features = [{"rms": 1}]
            companion.manual_history = [("TP", None)]
            companion.airflow = {"current": "drum", "history": [{"position": "drum"}]}
            companion.armed_started_monotonic = 1
            companion.precharge_capture = [(1, 410, 300)]
            active_reference_id = companion.references.active_id
            self.assertFalse(companion.state()["auto_start_armed"])

            companion.new_roast()

            self.assertTrue(saved.exists())
            self.assertEqual(companion.meta["bean"], "Preserved")
            self.assertEqual(companion.temperatures, {"et": 410, "bt": 300})
            self.assertEqual(companion.sensors, [sensor])
            self.assertTrue(companion.connected)
            self.assertEqual(companion.references.active_id, active_reference_id)
            self.assertEqual((companion.analyzer.points, companion.analyzer.events), ([], {}))
            self.assertEqual((companion.audio_features, companion.manual_history), ([], []))
            self.assertEqual(companion.operator_actions, {"confirmed": [], "history": []})
            self.assertEqual(companion.airflow, {"current": "drum", "history": []})
            self.assertIsNone(companion.last_saved)
            self.assertIsNone(companion.dropped_monotonic)
            self.assertIsNone(companion.started_monotonic)
            self.assertEqual(companion.precharge_capture, [])
            self.assertIsNone(companion.armed_started_monotonic)
            self.assertFalse(companion.recording_armed)
            self.assertFalse(companion.state()["auto_start_armed"])
            self.assertEqual(json.loads(companion.log_path.read_text().splitlines()[-1])["event"], "NEW_ROAST_READY")

    def test_new_roast_clears_arm_and_rejects_active_sessions(self):
        with isolated_companion() as (companion, _data_root):
            companion.connected = True
            companion.precharge_capture = [(1, 400, 300)]
            companion.start_recording()
            self.assertEqual(len(companion.precharge_capture), 0)
            self.assertTrue(companion.state()["auto_start_armed"])
            companion.new_roast()
            self.assertFalse(companion.state()["auto_start_armed"])
            companion._begin_recording(0)
            with self.assertRaisesRegex(ValueError, "Stop the active roast"):
                companion.new_roast()

    def test_next_roast_queue_isolated_through_arm_record_complete_cancel_and_promotion(self):
        with isolated_companion() as (companion, data_root):
            companion.update_meta({
                "bean": "Roast A", "origin": "Sumatra", "level": "light",
                "batch_g": 500, "desired_drop_f": 390,
            })
            active_a = companion.meta.copy()
            companion.connected = True
            companion.temperatures = {"et": 420, "bt": 410}
            companion.start_recording()
            companion.update_next_meta({
                "bean": "Roast B", "origin": "Panama", "level": "light",
                "batch_g": 300, "desired_drop_f": 400,
            })
            queued_b = companion.next_meta.copy()
            self.assertEqual(companion.meta, active_a)
            self.assertEqual(companion.analyzer.profile.drop_target_f, 390)

            before_invalid = companion.next_meta.copy()
            with self.assertRaisesRegex(ValueError, "250°F to 500°F"):
                companion.update_next_meta({"bean": "Invalid", "desired_drop_f": 501})
            self.assertEqual(companion.next_meta, before_invalid)

            cancelled = companion.cancel_arm()
            self.assertFalse(cancelled["auto_start_armed"])
            self.assertEqual((companion.meta, companion.next_meta), (active_a, queued_b))

            companion.start_recording()
            companion.mark("CHARGE")
            companion.update_next_meta({"notes": "Queued while A records"})
            queued_b = companion.next_meta.copy()
            self.assertEqual(companion.meta, active_a)
            companion.analyzer.add_point(10, 405, 390, auto=False)
            roast_id = companion.stop_recording()
            saved_a = json.loads((data_root / "roasts" / roast_id / "roast.json").read_text())
            self.assertEqual(saved_a["meta"], active_a)
            self.assertEqual(companion.meta, active_a)
            self.assertEqual(companion.next_meta, queued_b)

            companion.update_next_meta({"notes": "Queued after A completes"})
            queued_b = companion.next_meta.copy()
            self.assertEqual(companion.meta, active_a)
            companion.new_roast()
            self.assertEqual(companion.meta, queued_b)
            self.assertIsNone(companion.next_meta)
            self.assertEqual(companion.analyzer.profile.drop_target_f, 400)

    def test_roast_train_crud_reload_promotion_and_active_isolation(self):
        with isolated_companion() as (companion, data_root):
            companion.update_meta({
                "bean": "Active A", "origin": "Rwanda", "level": "light",
                "batch_g": 500, "desired_drop_f": 400,
            })
            active = companion.meta.copy()
            for bean, origin, target in (
                ("roast b", "panama", 401),
                ("Roast C", "Peru", 402),
                ("Roast D", "Sumatra", 403),
            ):
                companion.add_queue_item({
                    "bean": bean, "origin": origin, "level": "light",
                    "batch_g": 300, "desired_drop_f": target,
                })
            initial = companion.state()["roast_queue"]
            ids = {item["meta"]["bean"]: item["id"] for item in initial}
            self.assertEqual([item["meta"]["bean"] for item in initial], ["Roast B", "Roast C", "Roast D"])
            self.assertEqual(initial[0]["meta"]["origin"], "Panama")
            self.assertEqual(companion.state()["next_meta"], initial[0]["meta"])
            self.assertEqual(companion.state()["next_profile"], initial[0]["profile"])

            companion.move_queue_item({"id": ids["Roast C"], "direction": -1})
            companion.update_queue_item({
                "id": ids["Roast B"], "meta": {"origin": "sumatra", "notes": "Updated"},
            })
            train_path = data_root / "roast_train.json"
            before_invalid = (json.loads(json.dumps(companion.roast_queue)), train_path.read_bytes())
            with self.assertRaisesRegex(ValueError, "cannot move past"):
                companion.move_queue_item({"id": ids["Roast C"], "direction": -1})
            with self.assertRaisesRegex(ValueError, "250°F to 500°F"):
                companion.update_queue_item({"id": ids["Roast B"], "meta": {"desired_drop_f": 501}})
            self.assertEqual((companion.roast_queue, train_path.read_bytes()), before_invalid)
            companion.remove_queue_item({"id": ids["Roast D"]})
            self.assertEqual(companion.meta, active)

            reloaded = server.Companion()
            restored = reloaded.state()["roast_queue"]
            self.assertEqual(
                [(item["id"], item["meta"]["bean"]) for item in restored],
                [(ids["Roast C"], "Roast C"), (ids["Roast B"], "Roast B")],
            )
            self.assertEqual(restored[1]["meta"]["origin"], "Sumatra")
            reloaded.new_roast()
            promoted = reloaded.state()
            self.assertEqual(promoted["meta"]["bean"], "Roast C")
            self.assertEqual(
                [(item["id"], item["meta"]["bean"]) for item in promoted["roast_queue"]],
                [(ids["Roast B"], "Roast B")],
            )
            self.assertEqual(promoted["next_meta"], promoted["roast_queue"][0]["meta"])

    def test_roast_train_rejects_entry_51_atomically(self):
        with isolated_companion() as (companion, data_root):
            for index in range(50):
                companion.add_queue_item({
                    "bean": f"Batch {index + 1}", "origin": "Peru", "level": "light",
                    "batch_g": 300, "desired_drop_f": 400,
                })
            train_path = data_root / "roast_train.json"
            before = (json.loads(json.dumps(companion.roast_queue)), train_path.read_bytes())
            with self.assertRaisesRegex(ValueError, "limited to 50"):
                companion.add_queue_item({
                    "bean": "Batch 51", "origin": "Peru", "level": "light",
                    "batch_g": 300, "desired_drop_f": 400,
                })
            self.assertEqual((companion.roast_queue, train_path.read_bytes()), before)
            self.assertEqual(len(companion.state()["roast_queue"]), 50)

    def test_charge_guidance_and_precharge_capture_persist_without_csv_rows(self):
        with isolated_companion() as (companion, data_root):
            companion.connected = True
            companion.update_meta({"batch_g": 300})
            companion.temperatures = {"et": 420, "bt": 415}
            with patch.object(server.time, "monotonic", side_effect=(50, 52)):
                companion.start_recording()
                companion.mark("CHARGE")
            companion.analyzer.add_point(10, 430, 350, auto=False)
            roast_id = companion.stop_recording()
            roast = json.loads((data_root / "roasts" / roast_id / "roast.json").read_text())
            self.assertEqual(roast["precharge_points"], [{"elapsed": 0, "et": 420, "bt": 415}, {"elapsed": 2, "et": 420, "bt": 415}])
            self.assertEqual(
                roast["charge_guidance"],
                {"sensor": "bt", "target_f": 415.0, "low_f": 350.0, "high_f": 440.0, "basis": "Diedrich IR-Series starting baseline", "experimental_underload": True, "batch_adjustment_applied": False},
            )
            csv_rows = (data_root / "roasts" / roast_id / "temperatures.csv").read_text().splitlines()
            self.assertEqual(len(csv_rows), len(companion.analyzer.points) + 1)

            companion.new_roast()
            companion.update_meta({"level": "correction", "batch_g": 453})
            self.assertEqual(
                companion.state()["charge_guidance"],
                {"sensor": "et", "target_f": None, "low_f": 285.0, "high_f": 300.0, "basis": "Existing correction experiment envelope", "experimental_underload": False, "batch_adjustment_applied": False},
            )
            companion.recording = False
            companion.replaying = True
            with self.assertRaisesRegex(ValueError, "Stop the active roast"):
                companion.new_roast()

    def test_replay_sets_post_drop_time_without_saving(self):
        with isolated_companion() as (companion, data_root):
            companion.last_saved = "previous-live-roast"
            companion.dropped_monotonic = 1
            companion.start_replay(120)
            with patch.object(companion, "_save", wraps=companion._save) as save:
                companion._run_replay(1_000_000_000)
            self.assertFalse(companion.recording)
            self.assertFalse(companion.replaying)
            self.assertIn("DROP", companion.analyzer.events)
            self.assertIsNone(companion.last_saved)
            self.assertIsNotNone(companion.dropped_monotonic)
            self.assertEqual(save.call_count, 0)
            with self.assertRaisesRegex(ValueError, "no active recording"):
                companion.stop_recording()
            self.assertEqual(list((data_root / "roasts").iterdir()), [])

    def test_replay_stop_rejects_and_cancel_returns_ready_without_saved_mutation(self):
        roast = {
            "meta": {"origin": "Peru", "bean": "Read only", "level": "light", "batch_g": 300},
            "points": [{"elapsed": 0, "et": 400, "bt": 380}, {"elapsed": 2, "et": 410, "bt": 390}],
            "events": [{"name": "CHARGE", "elapsed": 0, "et": 400, "bt": 380}],
        }
        with isolated_companion([("saved", roast)]) as (companion, data_root):
            companion.set_saved_reference(["saved"])
            companion.add_queue_item({
                "bean": "Queued B", "origin": "Panama", "level": "light",
                "batch_g": 300, "desired_drop_f": 400,
            })
            companion.add_queue_item({
                "bean": "Queued C", "origin": "Peru", "level": "medium",
                "batch_g": 500, "desired_drop_f": 405,
            })
            queue_before = [
                (item["id"], item["meta"].copy()) for item in companion.state()["roast_queue"]
            ]
            train_before = (data_root / "roast_train.json").read_bytes()
            active_reference = companion.references.active_id
            roasts_root = data_root / "roasts"
            saved_files = {path.relative_to(roasts_root): path.read_bytes() for path in roasts_root.rglob("*") if path.is_file()}
            companion.start_replay(120, "saved")
            with patch.object(companion, "_save", wraps=companion._save) as save:
                with self.assertRaisesRegex(ValueError, "Cancel the replay"):
                    companion.stop_recording()
                self.assertTrue(companion.replaying)
                state = companion.cancel_replay()
            self.assertEqual(save.call_count, 0)
            self.assertEqual((state["recording"], state["replaying"], state["auto_start_armed"], state["last_saved"]), (False, False, False, None))
            self.assertEqual((companion.replay_source, companion.analyzer.points, companion.analyzer.events), (None, [], {}))
            self.assertEqual((state["saved_reference_roast_ids"], state["active_reference_id"]), (["saved"], active_reference))
            self.assertEqual(
                [(item["id"], item["meta"]) for item in state["roast_queue"]], queue_before,
            )
            self.assertEqual((data_root / "roast_train.json").read_bytes(), train_before)
            self.assertEqual({path.relative_to(roasts_root): path.read_bytes() for path in roasts_root.rglob("*") if path.is_file()}, saved_files)

    def test_cancel_during_replay_wait_cannot_restore_points_or_events(self):
        class CancelDuringWait:
            def __init__(self, companion):
                self.companion = companion
                self.cancelled = False

            def is_set(self):
                return False

            def wait(self, _seconds):
                if not self.cancelled:
                    self.cancelled = True
                    self.companion.cancel_replay()
                return False

        saved = {
            "meta": {"origin": "Peru", "bean": "Cancel race", "level": "light", "batch_g": 300},
            "points": [{"elapsed": 0, "et": 400, "bt": 380}, {"elapsed": 85.46, "et": 410, "bt": 390}],
            "events": [{"name": "CHARGE", "elapsed": 0, "et": 400, "bt": 380}],
        }
        for roast_id, roasts in ((None, None), ("saved", [("saved", saved)])):
            with self.subTest(source=roast_id or "static"), isolated_companion(roasts) as (companion, _data_root):
                companion.start_replay(120, roast_id)
                companion.stop = CancelDuringWait(companion)
                companion._run_replay(120)
                state = companion.state()
                self.assertEqual((companion.analyzer.points, companion.analyzer.events), ([], {}))
                self.assertEqual((state["recording"], state["replaying"], state["elapsed"]), (False, False, 0.0))

    def test_explicit_postcharge_replay_selects_exact_roast_and_fallback_stays_newest_eligible(self):
        old = {
            "meta": {"origin": "Peru", "bean": "Older", "level": "light", "batch_g": 300},
            "points": [
                {"elapsed": 0, "et": 400, "bt": 380},
                {"elapsed": 2, "et": 410, "bt": 390},
            ],
            "events": [
                {"name": "CHARGE", "elapsed": 0, "et": 400, "bt": 380, "source": "saved"},
                {"name": "DROP", "elapsed": 2, "et": 410, "bt": 390, "source": "saved"},
            ],
        }
        newest = {
            **old,
            "meta": {**old["meta"], "bean": "Newest eligible"},
            "precharge_points": [{"elapsed": 0, "et": 380, "bt": 415}],
        }
        with isolated_companion([("20260101_000000", old), ("20260102_000000", newest)]) as (companion, data_root):
            saved = data_root / "roasts" / "20260101_000000" / "roast.json"
            before = saved.read_bytes()
            companion.start_replay(1_000_000_000, "20260101_000000")
            self.assertEqual((companion.replay_source["id"], companion.recording_armed), ("20260101_000000", False))
            companion._run_replay(1_000_000_000)
            self.assertEqual((companion.analyzer.events["CHARGE"]["source"], companion.analyzer.events["DROP"]["source"]), ("saved", "saved"))
            self.assertEqual(saved.read_bytes(), before)
            companion.start_replay(120)
            self.assertEqual(companion.replay_source["id"], "20260102_000000")

    def test_saved_reference_single_and_average_follow_charge_alignment_contract(self):
        first = {
            "meta": {"origin": "Peru", "bean": "One", "level": "light", "batch_g": 300},
            "points": [{"elapsed": t, "et": et, "bt": bt} for t, et, bt in ((10, 100, 200), (12, 120, 220), (14, 140, 240))],
            "events": [
                {"name": name, "elapsed": t, "et": et, "bt": bt, "confidence": 0.8}
                for name, t, et, bt in (("CHARGE", 10, 100, 200), ("TP", 12, 120, 220), ("FCs", 14, 140, 240))
            ],
        }
        second = {
            "meta": {"origin": "Kenya", "bean": "Two", "level": "medium", "batch_g": 500},
            "points": [{"elapsed": t, "et": et, "bt": bt} for t, et, bt in ((5, 200, 300), (7, 240, 340), (8, 260, 360))],
            "events": [
                {"name": name, "elapsed": t, "et": et, "bt": bt, "confidence": 0.6}
                for name, t, et, bt in (("CHARGE", 5, 200, 300), ("TP", 6, 220, 320), ("DROP", 8, 260, 360))
            ],
        }
        roasts = [("one", first), ("two", second)]
        with isolated_companion(roasts) as (companion, data_root):
            files = [data_root / "roasts" / roast_id / "roast.json" for roast_id, _payload in roasts]
            before = [path.read_bytes() for path in files]
            companion.set_saved_reference(["one"])
            single = companion.state()
            self.assertEqual(single["saved_reference_roast_ids"], ["one"])
            self.assertEqual([point["elapsed"] for point in single["reference_overlay"]["points"]], [0.0, 2.0, 4.0])
            self.assertEqual(single["reference_overlay"]["source"], "Saved roast one")

            companion.set_saved_reference(["one", "two"])
            averaged = companion.state()
            overlay = averaged["reference_overlay"]
            self.assertEqual(averaged["saved_reference_roast_ids"], ["one", "two"])
            self.assertEqual(overlay["saved_roast_ids"], ["one", "two"])
            self.assertEqual(overlay["points"], [
                {"elapsed": 0, "et": 150.0, "bt": 250.0}, {"elapsed": 1, "et": 165.0, "bt": 265.0},
                {"elapsed": 2, "et": 180.0, "bt": 280.0}, {"elapsed": 3, "et": 195.0, "bt": 295.0},
            ])
            self.assertEqual([(event["name"], event["elapsed"], event["et"], event["bt"]) for event in overlay["events"]],
                             [("CHARGE", 0.0, 150.0, 250.0), ("TP", 1.5, 170.0, 270.0)])
            self.assertEqual([path.read_bytes() for path in files], before)
            imported_id = next(reference_id for reference_id in companion.references.references if not reference_id.startswith("saved-roast"))
            companion.set_active_reference(imported_id)
            self.assertEqual(companion.state()["saved_reference_roast_ids"], [])

    def test_reusable_taught_profile_selection_queues_exact_source_identity_and_reference(self):
        source = {
            "id": "teach-source",
            "meta": {
                "bean": "Boquete", "origin": "Panama", "level": "light", "notes": "",
                "batch_g": 300, "desired_drop_f": 400, "teach_mode": True,
                "roast_level_label": "City Plus", "learned_profile_id": None,
            },
            "profile": {"drop_target_f": 400},
            "points": [{"elapsed": 0, "et": 430, "bt": 420}, {"elapsed": 300, "et": 440, "bt": 400.5}],
            "events": [
                {"name": "CHARGE", "elapsed": 0, "et": 430, "bt": 420, "source": "automatic BT drop"},
                {
                    "name": "DROP", "elapsed": 300, "et": 440, "bt": 400.5,
                    "source": "automatic physical dump response",
                },
            ],
        }
        source["learning"] = with_color_confirmation(build_learning_block("teach-source", source), "hit")
        with isolated_companion([("teach-source", source)]) as (companion, _data_root):
            state = companion.select_learned_profile({"profile_id": "taught-teach-source"})
            self.assertEqual(state["next_meta"], {
                "bean": "Boquete", "origin": "Panama", "level": "light", "notes": "",
                "batch_g": 300, "desired_drop_f": 400.0, "teach_mode": False,
                "roast_level_label": "City Plus", "learned_profile_id": "taught-teach-source",
            })
            self.assertEqual(companion.next_reference_roast_id, "teach-source")
            learned_id = state["roast_queue"][0]["id"]
            companion.add_queue_item({
                "bean": "Tail", "origin": "Peru", "level": "medium",
                "batch_g": 500, "desired_drop_f": 405,
            })
            companion.move_queue_item({"id": learned_id, "direction": 1})
            companion.update_queue_item({"id": learned_id, "meta": {"notes": "Still exact"}})
            preserved = next(item for item in companion.state()["roast_queue"] if item["id"] == learned_id)
            self.assertEqual(
                (preserved["meta"]["learned_profile_id"], preserved["reference_roast_id"]),
                ("taught-teach-source", "teach-source"),
            )
            reloaded = server.Companion()
            restored = next(item for item in reloaded.state()["roast_queue"] if item["id"] == learned_id)
            self.assertEqual(
                (restored["meta"]["learned_profile_id"], restored["reference_roast_id"]),
                ("taught-teach-source", "teach-source"),
            )
            reloaded.update_queue_item({"id": learned_id, "meta": {"batch_g": 301}})
            cleared = next(item for item in reloaded.state()["roast_queue"] if item["id"] == learned_id)
            self.assertEqual((cleared["meta"]["learned_profile_id"], cleared["reference_roast_id"]), (None, None))
            self.assertEqual(cleared["id"], learned_id)

    def test_saved_reference_and_replay_rejections_are_atomic(self):
        good = {
            "meta": {"level": "light"},
            "points": [{"elapsed": 0, "et": 400, "bt": 380}],
            "events": [{"name": "CHARGE", "elapsed": 0, "et": 400, "bt": 380}],
        }
        with isolated_companion([("good", good), ("bad", "{")]) as (companion, data_root):
            companion.set_saved_reference(["good"])
            baseline = (companion.references.active_id, companion.saved_reference_roast_ids.copy(), companion.references.active())
            for roast_ids in ((["good", "good"]), ["missing"], ["bad"], [], ["good", "bad", "missing"]):
                with self.subTest(roast_ids=roast_ids), self.assertRaises(ValueError):
                    companion.set_saved_reference(roast_ids)
                self.assertEqual((companion.references.active_id, companion.saved_reference_roast_ids, companion.references.active()), baseline)
            companion.recording = True
            with self.assertRaisesRegex(ValueError, "Stop the active roast"):
                companion.set_saved_reference(["good"])
            with self.assertRaisesRegex(ValueError, "Stop the active roast"):
                companion.start_replay(120, "good")
            companion.recording = False
            for roast_id in ("missing", "bad"):
                with self.subTest(replay=roast_id), self.assertRaises(ValueError):
                    companion.start_replay(120, roast_id)
                self.assertFalse(companion.replaying)
            self.assertTrue((data_root / "roasts" / "good" / "roast.json").is_file())

    def test_saved_replay_uses_saved_meta_guidance_and_effective_profile(self):
        roast = {
            "meta": {
                "bean": "Warmikuna",
                "origin": "Peru",
                "level": "light",
                "notes": "saved source",
                "batch_g": 300,
                "desired_drop_f": 404,
            },
            "precharge_points": [{"elapsed": 0, "et": 380, "bt": 415}],
            "points": [{"elapsed": 0, "et": 380, "bt": 415}],
            "events": [],
        }
        with isolated_companion([("20260102_000000", roast)]) as (companion, _data_root):
            companion.update_meta({"level": "medium", "batch_g": 500})
            companion.start_replay(120)
            state = companion.state()
            self.assertEqual(state["meta"], {
                **roast["meta"], "teach_mode": False, "roast_level_label": "", "learned_profile_id": None,
            })
            self.assertEqual(state["batch_guidance"], batch_guidance(300, "light"))
            self.assertEqual((state["profile"]["drop_target_f"], state["profile"]["drop_ceiling_f"]), (404.0, 405.0))

    def test_saved_replay_arms_then_unfolds_precharge_before_detecting_and_backdating_charge(self):
        precharge = [
            {"elapsed": elapsed, "et": et, "bt": bt}
            for elapsed, et, bt in (
                (21.81, 375.211, 413.173), (22.82, 375.705, 413.804),
                (23.82, 376.325, 414.331), (24.82, 376.836, 414.778),
                (25.83, 377.41, 415.279), (26.83, 377.901, 415.503),
                (27.84, 378.467, 415.253), (28.84, 378.935, 414.29),
                (29.85, 379.48, 412.868), (30.85, 379.799, 410.879),
                (31.85, 380.284, 408.435), (32.86, 380.794, 405.923),
                (33.86, 381.256, 402.999), (34.86, 381.72, 400.738),
            )
        ]
        roast = {
            "meta": {"level": "medium"},
            "precharge_points": precharge,
            "points": [{"elapsed": 0, "et": 377.901, "bt": 415.503}, {"elapsed": 20, "et": 390, "bt": 380}],
            "events": [
                {"name": "CHARGE", "elapsed": 0, "et": 377.901, "bt": 415.503, "source": "stored"},
                {"name": "TP", "elapsed": 20, "et": 390, "bt": 380, "source": "stored TP"},
                {"name": "DROP", "elapsed": 20.004, "et": 390, "bt": 380, "source": "stored DROP"},
                {"name": "SCe", "elapsed": 20.02, "et": 390, "bt": 380, "source": "beyond data"},
            ],
        }
        with isolated_companion([("20260102_000000", roast)]) as (companion, _data_root):
            companion.start_replay(120)
            armed = companion.state()
            self.assertEqual(
                (armed["replaying"], armed["auto_start_armed"], armed["recording"], armed["elapsed"]),
                (True, True, False, 0),
            )
            self.assertEqual(armed["precharge_points"], [])
            for point in precharge[:3]:
                companion._process_charge_sample(point["elapsed"], point["et"], point["bt"], preserve_replay=True)
            partial = companion.state()
            self.assertEqual(partial["precharge_points"], precharge[:3])
            self.assertEqual((partial["replaying"], partial["auto_start_armed"], partial["recording"]), (True, True, False))
            companion.precharge_capture.clear()

            companion._run_replay(1_000_000_000)

            self.assertEqual(companion.started_monotonic, 26.83)
            self.assertEqual((companion.analyzer.events["CHARGE"]["elapsed"], companion.analyzer.events["CHARGE"]["source"]), (0, "automatic BT drop"))
            self.assertEqual(companion.analyzer.events["TP"]["source"], "stored TP")
            self.assertEqual((companion.analyzer.events["DROP"]["elapsed"], companion.analyzer.events["DROP"]["source"]), (20.0, "stored DROP"))
            self.assertNotIn("SCe", companion.analyzer.events)
            self.assertEqual(companion.analyzer.points[-1]["elapsed"], 20)
            logs = [json.loads(line)["event"] for line in companion.log_path.read_text().splitlines()]
            self.assertIn("REPLAY_CHARGE_TRIGGER", logs)
            self.assertNotIn("REPLAY_CHARGE_MISSED", logs)

    def test_saved_replay_without_matching_precharge_stops_and_logs_missed_trigger(self):
        roast = {
            "meta": {"level": "light"},
            "precharge_points": [
                {"elapsed": index, "et": 400, "bt": bt}
                for index, bt in enumerate((300, 302, 299, 301, 298, 300, 302, 299, 301, 298))
            ],
            "points": [{"elapsed": 0, "et": 400, "bt": 300}, {"elapsed": 10, "et": 390, "bt": 280}],
            "events": [{"name": "CHARGE", "elapsed": 0, "et": 400, "bt": 300, "source": "stored"}],
        }
        with isolated_companion([("20260102_000000", roast)]) as (companion, _data_root):
            companion.start_replay(120)
            companion._run_replay(1_000_000_000)
            self.assertEqual((companion.replaying, companion.recording_armed, companion.recording), (False, False, False))
            self.assertEqual((companion.analyzer.points, companion.analyzer.events), ([], {}))
            logs = [json.loads(line)["event"] for line in companion.log_path.read_text().splitlines()]
            self.assertIn("REPLAY_CHARGE_MISSED", logs)
            self.assertNotIn("REPLAY_CHARGE_TRIGGER", logs)

    def test_target_approach_alert_is_one_shot_and_names_authoritative_target(self):
        analyzer = RoastAnalyzer("light", desired_drop_f=400)
        analyzer.mark_event("CHARGE", elapsed=0, et=420, bt=380, source="test")
        analyzer.mark_event("TP", elapsed=1, et=415, bt=378, source="test")
        for elapsed, bt in ((10, 380), (20, 384), (30, 388), (40, 392), (45, 398), (46, 399)):
            analyzer.add_point(elapsed, bt + 20, bt)
        warnings = [alert for alert in analyzer.alerts if alert["kind"] in {"dump_warning", "ceiling_warning"}]
        self.assertEqual(len(warnings), 1)
        self.assertIn("400°F", warnings[0]["message"])
        self.assertNotIn("5 SECONDS", warnings[0]["message"])
        self.assertEqual((analyzer.profile.drop_target_f, analyzer.profile.drop_ceiling_f), (400, 400))
        self.assertNotIn("DROP", analyzer.events)

    def test_airflow_thresholds_are_in_roast_order(self):
        analyzer = RoastAnalyzer("light")
        self.assertLess(270, analyzer.profile.fc_start_f - 10)
        self.assertLess(analyzer.profile.fc_start_f, analyzer.profile.drop_target_f)

    def test_public_references_are_cross_machine_and_available_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            library = ReferenceLibrary(Path(folder), ROOT / "reference-roast.json", ROOT / "bundled-references")
            self.assertEqual(len(library.references), 5)
            machines = {item["machine"] for item in library.summaries()}
            self.assertIn("Probat LG3", machines)
            self.assertIn("Diedrich IR-5 / same probes", machines)


if __name__ == "__main__":
    unittest.main()
