#!/usr/bin/env python3
"""Focused behavior-contract tests for hands-off operator instructions."""

import unittest

from operation_guide import build_timeline, next_instruction


class OperationGuideTests(unittest.TestCase):
    NEXT_ROAST = {
        "id": "train-b", "bean": "Mandheling", "origin": "Sumatra",
        "batch_g": 1000, "level": "medium", "drop_target_f": 405.0,
    }

    def _timeline(self, **overrides):
        values = {
            "recording": True,
            "auto_start_armed": False,
            "bt": 250,
            "events": {},
            "prediction": None,
            "fc_candidate_f": 362,
            "drop_target_f": 390,
            "drop_ceiling_f": 392,
            "post_drop_seconds": None,
            "profile_key": "light",
            "elapsed": 0,
            "batch_g": None,
        }
        values.update(overrides)
        return build_timeline(**values)

    def test_timeline_idle_charge_and_turning_point(self):
        idle = self._timeline(recording=False, auto_start_armed=True, bt=None)
        charged = self._timeline(events={"CHARGE": {}})
        turning = self._timeline(events={"CHARGE": {}, "TP": {}})

        self.assertEqual(idle["next_step_id"], "roast_start")
        self.assertIn("armed", idle["steps"][0]["detail"])
        self.assertEqual([item["status"] for item in charged["steps"][:3]], ["done", "done", "current"])
        self.assertEqual(charged["next_step_id"], "tp")
        self.assertEqual(turning["next_step_id"], "air_half")
        self.assertTrue(all(sum(item["status"] == "current" for item in result["steps"]) == 1 for result in (idle, charged, turning)))

    def test_timeline_normal_airflow_and_first_crack_thresholds(self):
        events = {"CHARGE": {}, "TP": {}}
        half_distances = [self._timeline(bt=bt, events=events) for bt in (199, 265, 269, 270)]
        yellow = half_distances[2]
        half_reached = self._timeline(bt=270, events=events)
        drum_near = self._timeline(bt=351, events=events)
        drum_reached = self._timeline(bt=352, events=events)
        crack_window = self._timeline(bt=362, events=events)
        first_crack = self._timeline(bt=362, events={**events, "FCs": {}})

        self.assertEqual([item["next_summary"]["remaining_f"] for item in half_distances[:3]], [71.0, 5.0, 1.0])
        self.assertEqual(half_distances[0]["next_summary"], {"label": "Air → 50/50", "remaining_f": 71.0, "remaining_seconds": None})
        self.assertEqual(yellow["next_step_id"], "air_half")
        self.assertEqual(half_reached["next_step_id"], "air_drum")
        self.assertEqual(half_reached["next_summary"]["remaining_f"], 82.0)
        self.assertEqual(next(item["remaining_f"] for item in half_reached["steps"] if item["id"] == "air_half"), 0.0)
        self.assertEqual(drum_near["next_summary"]["remaining_f"], 1.0)
        self.assertEqual(drum_reached["next_step_id"], "fcs")
        self.assertEqual(drum_reached["next_summary"]["remaining_f"], 10.0)
        self.assertEqual((crack_window["next_step_id"], crack_window["next_summary"]["remaining_f"]), ("fcs", 0.0))
        crack_detail = next(item["detail"] for item in crack_window["steps"] if item["id"] == "fcs")
        self.assertIn("curve/listening", crack_detail.lower())
        self.assertEqual(first_crack["next_step_id"], "dump")
        self.assertEqual(first_crack["next_summary"], {"label": "Pilot only + Dump", "remaining_f": 28.0, "remaining_seconds": None})
        self.assertEqual(next(item["remaining_f"] for item in crack_window["steps"] if item["id"] == "dump"), 28.0)
        for step_id in ("air_half", "air_drum"):
            detail = next(item["detail"] for item in yellow["steps"] if item["id"] == step_id)
            self.assertIn("cue", detail)
            self.assertIn("not sensed airflow confirmation", detail)

        pilot = self._timeline(bt=362, events={**events, "FCs": {}}, prediction={"target_temp_f": 390, "seconds_to_drop": 25})
        dump = self._timeline(bt=362, events={**events, "FCs": {}}, prediction={"target_temp_f": 390, "seconds_to_drop": 20})
        self.assertEqual(pilot["next_summary"], {"label": "Pilot only + Dump", "remaining_f": 28.0, "remaining_seconds": 25.0})
        self.assertEqual(dump["next_step_id"], "dump")
        self.assertEqual(dump["next_summary"], {"label": "Pilot only + Dump", "remaining_f": 28.0, "remaining_seconds": 20.0})

    def test_timeline_prediction_drop_cooling_and_zero_clamping(self):
        events = {"CHARGE": {}, "TP": {}, "FCs": {}}
        predicted = self._timeline(bt=385, events=events, prediction={"target_temp_f": 388, "seconds_to_drop": 14})
        dump = next(item for item in predicted["steps"] if item["id"] == "dump")
        self.assertEqual(predicted["next_step_id"], "dump")
        self.assertEqual((dump["remaining_f"], dump["remaining_seconds"]), (5.0, 14.0))
        self.assertEqual(predicted["next_summary"], {"label": "Pilot only + Dump", "remaining_f": 5.0, "remaining_seconds": 14.0})

        clamped = self._timeline(bt=395, events=events, prediction={"target_temp_f": 390, "seconds_to_drop": -2})
        clamped_dump = next(item for item in clamped["steps"] if item["id"] == "dump")
        self.assertEqual((clamped_dump["remaining_f"], clamped_dump["remaining_seconds"]), (0.0, 0.0))

        dropped = self._timeline(bt=395, events={**events, "DROP": {}}, post_drop_seconds=20)
        cooled = self._timeline(bt=395, events={**events, "DROP": {}}, post_drop_seconds=60)
        self.assertEqual(dropped["next_step_id"], "cooling")
        self.assertEqual(dropped["next_summary"]["remaining_seconds"], 40.0)
        self.assertIsNone(cooled["next_step_id"])
        self.assertIsNone(cooled["next_summary"])
        self.assertTrue(all(item["status"] == "done" for item in cooled["steps"]))

    def test_timeline_drop_closes_missing_event_windows_honestly(self):
        cases = (
            ({"CHARGE": {}, "DROP": {}}, ("tp", "fcs")),
            ({"CHARGE": {}, "TP": {}, "DROP": {}}, ("fcs",)),
        )
        for events, missing in cases:
            with self.subTest(events=events):
                cooling = self._timeline(events=events, post_drop_seconds=20)
                cooled = self._timeline(events=events, post_drop_seconds=60)
                self.assertEqual(cooling["next_step_id"], "cooling")
                self.assertIsNone(cooled["next_step_id"])
                self.assertTrue(all(item["status"] == "done" for item in cooling["steps"][:-1]))
                for timeline in (cooling, cooled):
                    for step_id in missing:
                        detail = next(item["detail"] for item in timeline["steps"] if item["id"] == step_id)
                        self.assertIn("no recorded", detail)

        correction = self._timeline(
            profile_key="correction", events={"CHARGE": {}, "DROP": {}}, post_drop_seconds=20
        )
        correction_cooled = self._timeline(
            profile_key="correction", events={"CHARGE": {}, "DROP": {}}, post_drop_seconds=60
        )
        self.assertEqual(correction["next_step_id"], "cooling")
        self.assertIsNone(correction_cooled["next_step_id"])
        self.assertIn("no recorded TP", next(item["detail"] for item in correction["steps"] if item["id"] == "tp"))

    def test_timeline_correction_order_and_existing_schedule(self):
        events = {"CHARGE": {}, "TP": {}}
        common = {"profile_key": "correction", "events": events, "bt": 360, "drop_ceiling_f": 375}
        early = self._timeline(elapsed=100, **common)
        drum = self._timeline(elapsed=150, **common)
        cooling = self._timeline(elapsed=180, **common)
        self.assertEqual(early["next_summary"], {"label": "Air → Roast Drum", "remaining_f": 10.0, "remaining_seconds": 80.0})
        self.assertEqual(drum["next_summary"], {"label": "Air → Roast Drum", "remaining_f": 10.0, "remaining_seconds": 30.0})
        self.assertEqual(cooling["next_summary"], {"label": "Air → Cooling Bin", "remaining_f": 10.0, "remaining_seconds": 90.0})
        due = self._timeline(elapsed=270, **common)
        self.assertEqual(due["next_step_id"], "dump")
        self.assertEqual(due["next_summary"], {"label": "Pilot only + Dump", "remaining_f": 15.0, "remaining_seconds": 30.0})
        approach = self._timeline(elapsed=270, bt=370, **{k: v for k, v in common.items() if k != "bt"})
        self.assertEqual(approach["next_summary"], {"label": "Pilot only + Dump", "remaining_f": 5.0, "remaining_seconds": 30.0})
        temperature = self._timeline(elapsed=100, bt=370, **{k: v for k, v in common.items() if k != "bt"})
        self.assertEqual(temperature["next_summary"], {"label": "Pilot only + Dump", "remaining_f": 5.0, "remaining_seconds": 200.0})
        self.assertEqual(next(item["remaining_seconds"] for item in due["steps"] if item["id"] == "air_cooling"), 0.0)
        ceiling = self._timeline(elapsed=100, bt=375, **{k: v for k, v in common.items() if k != "bt"})
        self.assertEqual((ceiling["next_step_id"], ceiling["next_summary"]["remaining_f"]), ("dump", 0.0))
        self.assertEqual(
            [item["id"] for item in due["steps"]],
            ["roast_start", "charge", "tp", "air_half", "air_drum", "air_cooling", "dump", "cooling"],
        )

    def test_correction_time_phases_cover_every_boundary(self):
        common = {
            "recording": True, "bt": 360,
            "events": {"FCs": {}}, "prediction": {"seconds_to_drop": 1},
            "airflow_current": "drum", "fc_candidate_f": 362,
            "drop_target_f": 375,
            "post_drop_seconds": None, "confirmed_actions": set(),
            "profile_key": "correction",
            "ror": 10,
        }
        phases = (
            (0, "correction_equalize"),
            (89.9, "correction_equalize"),
            (90, "correction_ror"),
            (149.9, "correction_ror"),
            (150, "correction_pilot"),
            (179.9, "correction_pilot"),
            (180, "correction_sample"),
            (209.9, "correction_sample"),
            (210, "correction_window"),
            (269.9, "correction_window"),
            (270, "correction_prepare_dump"),
            (294.9, "correction_prepare_dump"),
            (295, "correction_final_approach"),
            (299.9, "correction_final_approach"),
            (300, "correction_time_limit"),
        )
        for elapsed, instruction_id in phases:
            with self.subTest(elapsed=elapsed):
                result = next_instruction(elapsed=elapsed, **common)
                self.assertEqual(result["id"], instruction_id)
                self.assertIn("Do not wait for FC", result["detail"])
                self.assertIn("manual Dump + save", result["detail"])

    def test_correction_idle_temperature_and_ror_priorities(self):
        common = {
            "recording": True, "events": {}, "prediction": None,
            "airflow_current": "half", "fc_candidate_f": 362,
            "drop_target_f": 375,
            "post_drop_seconds": None, "confirmed_actions": set(),
            "profile_key": "correction",
        }
        cases = (
            (360, 89, 20, "correction_equalize"),
            (369.9, 90, 15, "correction_ror"),
            (360, 90, 15.1, "correction_ror_high"),
            (370, 100, 10, "correction_temp_window"),
            (372.9, 100, 10, "correction_temp_window"),
            (373, 100, 10, "correction_final_approach"),
            (374.9, 100, 10, "correction_final_approach"),
            (374.9, 300, 20, "correction_time_limit"),
            (375, 100, 20, "correction_preferred_ceiling"),
            (379.9, 100, 20, "correction_preferred_ceiling"),
            (380, 300, 20, "correction_hard_ceiling"),
        )
        for bt, elapsed, ror, instruction_id in cases:
            with self.subTest(bt=bt, elapsed=elapsed, ror=ror):
                result = next_instruction(bt=bt, elapsed=elapsed, ror=ror, **common)
                self.assertEqual(result["id"], instruction_id)

        temperature_prep = next_instruction(bt=370, elapsed=100, ror=10, **common)
        self.assertIn("COOLING + AGITATOR", temperature_prep["title"])
        final_time = next_instruction(bt=360, elapsed=295, ror=10, **common)
        final_temp = next_instruction(bt=373, elapsed=100, ror=10, **common)
        self.assertEqual(final_time["title"], "PILOT ONLY · DUMP IN 5")
        self.assertIn("5s to the 300s limit", final_time["detail"])
        self.assertIn("2°F to the 375°F preferred ceiling", final_temp["detail"])

        high_ror = next_instruction(bt=360, elapsed=90, ror=15.1, **common)
        self.assertEqual(high_ror["title"], "REDUCE HEAT NOW")
        self.assertIn("dial the gas down", high_ror["detail"])
        self.assertNotIn("pilot only", high_ror["detail"].lower())

        idle = next_instruction(recording=False, bt=None, elapsed=0, ror=None, **{k: v for k, v in common.items() if k != "recording"})
        self.assertEqual(idle["id"], "correction_idle")
        self.assertIn("285–300°F", idle["title"])
        self.assertIn("control sample", idle["detail"])

    def test_correction_hopper_window_yields_to_existing_safety_cues(self):
        common = {
            "recording": True, "bt": 340, "events": {"CHARGE": {}, "TP": {}},
            "prediction": None, "airflow_current": "half", "fc_candidate_f": 362,
            "drop_target_f": 375, "post_drop_seconds": None, "confirmed_actions": set(),
            "profile_key": "correction", "ror": 10,
            "next_roast": self.NEXT_ROAST,
        }

        cases = (
            (149.9, "correction_ror"),
            (150, "hopper_next:train-b"),
            (179.9, "hopper_next:train-b"),
            (180, "correction_sample"),
        )
        for elapsed, instruction_id in cases:
            with self.subTest(elapsed=elapsed):
                self.assertEqual(next_instruction(elapsed=elapsed, **common)["id"], instruction_id)

        hopper = next_instruction(elapsed=160, **common)
        high_ror = next_instruction(elapsed=160, **dict(common, ror=15.1))
        hot = next_instruction(elapsed=160, **dict(common, bt=370))
        no_queue = next_instruction(
            elapsed=160,
            **{key: value for key, value in common.items() if key != "next_roast"},
        )

        self.assertEqual(hopper["confirm_actions"], [])
        self.assertIn("Next roast drop target 405°F", hopper["detail"])
        self.assertIn("not sensed", hopper["detail"])
        self.assertNotIn("loaded", hopper["detail"].lower())
        self.assertEqual(high_ror["id"], "correction_ror_high")
        self.assertEqual(hot["id"], "correction_temp_window")
        self.assertEqual(no_queue["id"], "correction_pilot")
        timeline_ids = [item["id"] for item in self._timeline(
            bt=340, events=common["events"], profile_key="correction", elapsed=149,
            next_roast=self.NEXT_ROAST,
        )["steps"]]
        self.assertLess(timeline_ids.index("hopper_next"), timeline_ids.index("air_drum"))

    def test_correction_optional_logs_never_change_instruction(self):
        common = {
            "recording": True,
            "bt": 365,
            "events": {},
            "prediction": None,
            "airflow_current": "half",
            "fc_candidate_f": 362,
            "drop_target_f": 375,
            "post_drop_seconds": None,
            "profile_key": "correction",
            "elapsed": 270,
            "ror": 10,
        }
        pending = next_instruction(confirmed_actions=set(), **common)
        logged = next_instruction(confirmed_actions={"pilot_only", "agitator_on"}, **common)
        early = next_instruction(confirmed_actions=set(), **dict(common, elapsed=150))
        final = next_instruction(confirmed_actions=set(), **dict(common, elapsed=295))

        self.assertEqual(early["confirm_actions"], [])
        self.assertEqual(
            pending["confirm_actions"],
            [{"id": "agitator_on", "label": "Log agitator ON"}],
        )
        self.assertEqual(
            final["confirm_actions"],
            [
                {"id": "pilot_only", "label": "Log pilot only"},
                {"id": "agitator_on", "label": "Log agitator ON"},
            ],
        )
        self.assertEqual(logged["confirm_actions"], [])
        self.assertEqual(
            (logged["id"], logged["title"], logged["detail"]),
            (pending["id"], pending["title"], pending["detail"]),
        )

    def test_correction_keeps_shared_post_drop_and_normal_defaults(self):
        normal_args = {
            "recording": True,
            "bt": 271,
            "events": {},
            "prediction": None,
            "airflow_current": "cooling",
            "fc_candidate_f": 362,
            "drop_target_f": 390,
            "post_drop_seconds": None,
            "confirmed_actions": set(),
        }
        self.assertEqual(
            next_instruction(**normal_args),
            next_instruction(profile_key="light", elapsed=100, ror=20, **normal_args),
        )

        post_drop = dict(normal_args, recording=False, bt=390, post_drop_seconds=20)
        normal = next_instruction(**post_drop)
        correction = next_instruction(profile_key="correction", elapsed=300, ror=20, **post_drop)
        self.assertEqual(correction, normal)

    def test_no_click_airflow_stages_advance_from_roast_state(self):
        common = {
            "recording": True,
            "events": {},
            "fc_candidate_f": 362,
            "drop_target_f": 390,
            "post_drop_seconds": None,
            "confirmed_actions": set(),
        }

        early = next_instruction(bt=250, prediction=None, airflow_current="drum", **common)
        yellow = next_instruction(bt=271, prediction=None, airflow_current="half", **common)
        first_crack = next_instruction(bt=353, prediction=None, airflow_current="drum", **common)
        predump = next_instruction(
            bt=385,
            prediction={"seconds_to_drop": 14},
            airflow_current="cooling",
            **common,
        )

        self.assertEqual(
            [early["id"], yellow["id"], first_crack["id"], predump["id"]],
            ["early_set_cooling", "yellow_set_half", "fc_set_drum", "predump_ready"],
        )
        self.assertIn("COOLING BIN", early["title"])
        self.assertIn("50 / 50", yellow["title"])
        self.assertIn("ROAST DRUM", first_crack["title"])
        self.assertEqual(predump["title"], "DUMP IN 5°F · TARGET 390°F")
        self.assertIn("Estimated 14s", predump["detail"])

    def test_300g_fuel_schedule_is_advisory_and_not_time_limited(self):
        common = {
            "recording": True,
            "bt": 220,
            "events": {"CHARGE": {"elapsed": 0}},
            "prediction": None,
            "airflow_current": "cooling",
            "fc_candidate_f": 362,
            "drop_target_f": 390,
            "post_drop_seconds": None,
            "confirmed_actions": set(),
            "profile_key": "light",
            "ror": 20,
            "batch_g": 300,
        }

        before_tp = next_instruction(elapsed=60, **common)
        at_tp = next_instruction(
            elapsed=60,
            **dict(common, events={"CHARGE": {"elapsed": 0}, "TP": {"elapsed": 60}}),
        )
        long_after_tp = next_instruction(
            elapsed=600,
            **dict(common, events={"CHARGE": {"elapsed": 0}, "TP": {"elapsed": 60}}),
        )

        self.assertEqual(
            (before_tp["fuel_guidance"]["band"], before_tp["fuel_guidance"]["range"], before_tp["fuel_guidance"]["preferred"]),
            ("high", [8, 8], 8),
        )
        self.assertEqual(
            (at_tp["fuel_guidance"]["band"], at_tp["fuel_guidance"]["range"], at_tp["fuel_guidance"]["preferred"]),
            ("mid", [6, 7], 6),
        )
        self.assertEqual(long_after_tp["fuel_guidance"], at_tp["fuel_guidance"])
        self.assertIn("300 g schedule: High 8 until TP, then Mid 6", at_tp["fuel_guidance"]["schedule_note"])
        self.assertEqual(before_tp["id"], at_tp["id"])
        self.assertNotIn("flame_six", [step["id"] for step in self._timeline(batch_g=300)["steps"]])

    def test_300g_ready_instruction_exposes_high_fuel_advice(self):
        ready = next_instruction(
            recording=False,
            bt=None,
            events={},
            prediction=None,
            airflow_current="cooling",
            fc_candidate_f=362,
            drop_target_f=390,
            post_drop_seconds=None,
            confirmed_actions=set(),
            batch_g=300,
        )

        self.assertEqual(ready["id"], "ready_small_batch")
        self.assertEqual(ready["title"], "READY · AIR → COOLING BIN")
        self.assertEqual(ready["fuel_guidance"]["band"], "high")
        self.assertEqual(ready["fuel_guidance"]["preferred"], 8)
        self.assertIn("High 8 until TP, then Mid 6", ready["fuel_guidance"]["schedule_note"])

    def test_fuel_guidance_progresses_high_mid_low_then_pilot_only(self):
        common = {
            "recording": True,
            "prediction": None,
            "airflow_current": "cooling",
            "fc_candidate_f": 362,
            "drop_target_f": 400,
            "post_drop_seconds": None,
            "confirmed_actions": set(),
        }
        instructions = (
            next_instruction(bt=250, events={"CHARGE": {}}, **common),
            next_instruction(bt=300, events={"CHARGE": {}, "TP": {}}, **common),
            next_instruction(bt=388, events={"CHARGE": {}, "TP": {}}, **common),
            next_instruction(bt=396, events={"CHARGE": {}, "TP": {}}, **common),
        )

        self.assertEqual(
            [
                (item["fuel_guidance"]["band"], item["fuel_guidance"]["range"], item["fuel_guidance"]["preferred"])
                for item in instructions
            ],
            [("high", [8, 8], 8), ("mid", [6, 7], 6), ("low", [4, 5], 5), ("pilot_only", None, None)],
        )
        self.assertFalse(instructions[2]["fuel_guidance"]["pilot_only"])
        self.assertTrue(instructions[3]["fuel_guidance"]["pilot_only"])
        self.assertEqual([item["target_temp_f"] for item in instructions], [400.0] * 4)
        self.assertEqual([item["degrees_to_target_f"] for item in instructions], [150.0, 100.0, 12.0, 4.0])
        for item in instructions:
            self.assertTrue(item["fuel_guidance"]["advisory"])
            self.assertIn("does not sense or apply", item["fuel_guidance"]["detail"])

    def test_manual_first_crack_is_observational_only(self):
        common = {
            "recording": True,
            "bt": 300,
            "prediction": None,
            "airflow_current": "half",
            "fc_candidate_f": 362,
            "drop_target_f": 400,
            "post_drop_seconds": None,
            "confirmed_actions": set(),
        }
        before = next_instruction(events={"CHARGE": {}, "TP": {}}, **common)
        manual = next_instruction(
            events={"CHARGE": {}, "TP": {}, "FCs": {"source": "manual button"}},
            **common,
        )
        automatic = next_instruction(
            events={"CHARGE": {}, "TP": {}, "FCs": {"source": "automatic curve detection"}},
            **common,
        )

        self.assertEqual(
            (manual["id"], manual["title"], manual["detail"], manual["fuel_guidance"]),
            (before["id"], before["title"], before["detail"], before["fuel_guidance"]),
        )
        self.assertEqual(before["id"], "yellow_set_half")
        self.assertEqual(automatic["id"], "fc_set_drum")

    def test_dump_copy_uses_authoritative_target_and_signed_delta(self):
        common = {
            "recording": True,
            "events": {"CHARGE": {}, "TP": {}},
            "prediction": None,
            "airflow_current": "drum",
            "fc_candidate_f": 362,
            "drop_target_f": 400,
            "post_drop_seconds": None,
            "confirmed_actions": set(),
        }
        approaching = next_instruction(bt=385, **common)
        at_target = next_instruction(bt=400, **common)
        past_target = next_instruction(bt=405, **common)

        self.assertEqual(approaching["title"], "DUMP IN 15°F · TARGET 400°F")
        self.assertEqual(approaching["degrees_to_target_f"], 15.0)
        self.assertIn("Current delta +15°F", approaching["detail"])
        for instruction, delta, position in (
            (at_target, 0.0, "BT is at the authoritative target"),
            (past_target, -5.0, "BT is 5°F past the authoritative target"),
        ):
            self.assertEqual(instruction["title"], "DUMP NOW · TARGET PASSED · TARGET 400°F")
            self.assertEqual(instruction["target_temp_f"], 400.0)
            self.assertEqual(instruction["degrees_to_target_f"], delta)
            self.assertIn(position, instruction["detail"])
            self.assertIn(f"current delta {delta:+g}°F", instruction["detail"])

    def test_normal_hopper_window_copy_identity_and_dump_priority(self):
        common = {
            "recording": True, "events": {"CHARGE": {}, "TP": {}},
            "prediction": None, "airflow_current": "cooling", "fc_candidate_f": 362,
            "drop_target_f": 400, "post_drop_seconds": None, "confirmed_actions": set(),
            "next_roast": self.NEXT_ROAST,
        }
        expected = {
            61: "yellow_set_half", 60: "hopper_next:train-b", 45: "hopper_next:train-b",
            30: "fc_set_drum", 15: "predump_ready", 0: "dump_target_passed",
        }
        results = {}
        for delta, instruction_id in expected.items():
            with self.subTest(delta=delta):
                results[delta] = next_instruction(bt=400 - delta, **common)
                self.assertEqual(results[delta]["id"], instruction_id)

        hopper = results[60]
        self.assertEqual(hopper["title"], "LOAD NEXT HOPPER NOW · MANDHELING · 1000 G")
        self.assertIn("Sumatra · Mandheling · Medium", hopper["detail"])
        self.assertIn("Next roast drop target 405°F", hopper["detail"])
        self.assertIn("Do not charge", hopper["detail"])
        self.assertIn("not sensed", hopper["detail"])
        self.assertNotIn("loaded", hopper["detail"].lower())
        self.assertEqual(hopper["confirm_actions"], [])
        self.assertEqual(results[45]["id"], hopper["id"])

        changed_head = next_instruction(
            bt=340,
            **dict(common, next_roast={**self.NEXT_ROAST, "id": "train-c"}),
        )
        before_tp = next_instruction(bt=340, **dict(common, events={"CHARGE": {}}))
        predicted_dump = next_instruction(bt=355, **dict(common, prediction={"seconds_to_drop": 10}))
        self.assertEqual(changed_head["id"], "hopper_next:train-c")
        self.assertNotEqual(changed_head["id"], hopper["id"])
        self.assertNotEqual(before_tp["id"], hopper["id"])
        self.assertEqual(predicted_dump["id"], "predump_ready")
        self.assertEqual(results[0]["title"], "DUMP NOW · TARGET PASSED · TARGET 400°F")

        without_queue = self._timeline(bt=339, events=common["events"], drop_target_f=400)
        with_queue = self._timeline(
            bt=339, events=common["events"], drop_target_f=400, next_roast=self.NEXT_ROAST,
        )
        self.assertNotIn("hopper_next", [item["id"] for item in without_queue["steps"]])
        queued_ids = [item["id"] for item in with_queue["steps"]]
        self.assertEqual(queued_ids.count("hopper_next"), 1)
        self.assertLess(queued_ids.index("hopper_next"), queued_ids.index("air_drum"))

    def test_instruction_identity_and_copy_ignore_logged_state(self):
        base = {
            "recording": True,
            "bt": 385,
            "events": {},
            "prediction": {"seconds_to_drop": 14},
            "fc_candidate_f": 362,
            "drop_target_f": 390,
            "post_drop_seconds": None,
        }
        variants = [
            next_instruction(airflow_current="drum", confirmed_actions=set(), **base),
            next_instruction(airflow_current="half", confirmed_actions={"agitator_on"}, **base),
            next_instruction(
                airflow_current="cooling",
                confirmed_actions={"agitator_on", "pilot_only"},
                **base,
            ),
        ]

        identity_and_copy = [(item["id"], item["title"], item["detail"]) for item in variants]
        self.assertEqual(identity_and_copy, [identity_and_copy[0]] * len(variants))

    def test_predump_exposes_all_unlogged_optional_actions_together(self):
        common = {
            "recording": True,
            "bt": 385,
            "events": {},
            "prediction": {"seconds_to_drop": 14},
            "airflow_current": "drum",
            "fc_candidate_f": 362,
            "drop_target_f": 390,
            "post_drop_seconds": None,
        }

        none_logged = next_instruction(confirmed_actions=set(), **common)
        agitator_logged = next_instruction(confirmed_actions={"agitator_on"}, **common)
        all_logged = next_instruction(confirmed_actions={"agitator_on", "pilot_only"}, **common)

        self.assertEqual(
            none_logged["confirm_actions"],
            [
                {"id": "agitator_on", "label": "Log agitator ON"},
                {"id": "pilot_only", "label": "Log pilot only"},
            ],
        )
        self.assertEqual(
            agitator_logged["confirm_actions"],
            [{"id": "pilot_only", "label": "Log pilot only"}],
        )
        self.assertEqual(all_logged["confirm_actions"], [])
        self.assertNotIn("confirm_action", none_logged)
        self.assertIn("Cooling Bin", none_logged["detail"])
        self.assertIn("agitator ON", none_logged["detail"])
        self.assertIn("PILOT ONLY", none_logged["detail"])
        self.assertIn("optional", none_logged["detail"])

    def test_post_drop_guidance_keeps_full_cooling_and_agitation(self):
        common = {
            "recording": False,
            "bt": 390,
            "events": {"DROP": {}},
            "prediction": None,
            "airflow_current": "cooling",
            "fc_candidate_f": 362,
            "drop_target_f": 390,
        }

        holding = next_instruction(post_drop_seconds=20, confirmed_actions=set(), **common)
        cooled = next_instruction(post_drop_seconds=61, confirmed_actions=set(), **common)

        self.assertEqual(holding["id"], "post_drop_cooling")
        self.assertEqual(cooled["id"], "post_drop_cooled")
        self.assertEqual((holding["urgency"], cooled["urgency"]), ("active", "ready"))
        for instruction in (holding, cooled):
            copy = f'{instruction["title"]} {instruction["detail"]}'.upper()
            self.assertNotIn("STOP", copy)
            self.assertNotIn("SPREAD", copy)
            self.assertIn("COOLING", copy)
            self.assertIn("AGITATOR ON", copy)
            self.assertEqual(instruction["confirm_actions"], [])


if __name__ == "__main__":
    unittest.main()
