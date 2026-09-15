#!/usr/bin/env python3

import unittest

from learning_profiles import (
    AUTOMATIC_PHYSICAL_DROP_SOURCE,
    build_learning_block,
    is_eligible,
    learning_status,
    match_kind,
    profile_summary,
    saved_roast_summaries,
    with_color_confirmation,
)


def teach_roast(*, drop_source=AUTOMATIC_PHYSICAL_DROP_SOURCE, observed=400.5, desired=400):
    return {
        "meta": {
            "bean": "Boquete",
            "origin": "Panama",
            "batch_g": 300,
            "roast_level_label": "City Plus",
            "desired_drop_f": desired,
        },
        "profile": {"drop_target_f": desired},
        "points": [{"elapsed": 0, "et": 430, "bt": 420}],
        "events": [
            {
                "name": "DROP",
                "elapsed": 300,
                "et": 440,
                "bt": observed,
                "source": drop_source,
            }
        ],
    }


class LearningProfileTests(unittest.TestCase):
    def test_automatic_physical_drop_starts_pending_without_copying_curve(self):
        roast = teach_roast()
        learning = build_learning_block("source-1", roast)
        self.assertTrue(is_eligible(learning))
        self.assertEqual(learning_status(learning), "pending")
        self.assertEqual(learning["profile_id"], "taught-source-1")
        self.assertEqual(learning["source_roast_id"], "source-1")
        self.assertEqual(learning["color_confirmation"]["state"], "unconfirmed")
        self.assertNotIn("points", learning)
        self.assertNotIn("events", learning)

    def test_hit_within_one_degree_learns_desired_target(self):
        learning = with_color_confirmation(build_learning_block("source-1", teach_roast()), "hit")
        self.assertEqual(learning_status(learning), "reusable")
        self.assertEqual(learning["target_evidence"]["learned_drop_f"], 400)

    def test_hit_outside_one_degree_learns_observed_drop(self):
        learning = with_color_confirmation(
            build_learning_block("source-2", teach_roast(observed=403.25)), "hit", "Good color"
        )
        self.assertEqual(learning_status(learning), "reusable")
        self.assertEqual(learning["target_evidence"]["learned_drop_f"], 403.25)
        self.assertEqual(learning["color_confirmation"]["notes"], "Good color")

    def test_miss_and_skip_are_not_reusable(self):
        pending = build_learning_block("source-1", teach_roast())
        missed = with_color_confirmation(pending, "miss")
        skipped = with_color_confirmation(pending, "skip")
        self.assertEqual(learning_status(missed), "rejected")
        self.assertEqual(learning_status(skipped), "pending")
        self.assertIsNone(missed["target_evidence"]["learned_drop_f"])
        self.assertIsNone(skipped["target_evidence"]["learned_drop_f"])

    def test_manual_drop_and_legacy_schema_are_ineligible(self):
        manual = build_learning_block("source-1", teach_roast(drop_source="Stop / Dump button"))
        legacy = build_learning_block("source-2", teach_roast())
        legacy["schema_version"] = 0
        self.assertFalse(is_eligible(manual))
        self.assertEqual(learning_status(manual), "ineligible")
        self.assertFalse(is_eligible(legacy))
        self.assertEqual(learning_status(legacy), "ineligible")

    def test_exact_match_normalizes_text_and_rejects_cross_identity_reuse(self):
        learning = with_color_confirmation(build_learning_block("source-1", teach_roast()), "hit")
        summary = profile_summary("source-1", {"learning": learning})
        exact = {"bean": "  BOQUETE ", "origin": "panama", "batch_g": 300, "roast_level_label": "city plus"}
        self.assertEqual(match_kind(summary, exact), "exact")
        self.assertEqual(match_kind(summary, {**exact, "batch_g": 301}), "suggestion")
        self.assertEqual(match_kind(summary, {**exact, "roast_level_label": "Light"}), "suggestion")
        self.assertEqual(match_kind(summary, {**exact, "bean": "Gesha"}), "none")
        self.assertEqual(match_kind(summary, {**exact, "origin": "Peru"}), "none")

    def test_saved_summaries_include_learning_only_and_preserve_source_reference(self):
        learning = with_color_confirmation(build_learning_block("source-1", teach_roast()), "hit")
        summaries = saved_roast_summaries(
            [("source-1", {"learning": learning, "points": ["not copied"]}), ("plain", teach_roast())]
        )
        self.assertEqual(len(summaries), 1)
        self.assertEqual(
            (summaries[0]["source_roast_id"], summaries[0]["profile_id"], summaries[0]["status"]),
            ("source-1", "taught-source-1", "reusable"),
        )
        self.assertNotIn("points", summaries[0])


if __name__ == "__main__":
    unittest.main()
