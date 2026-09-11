#!/usr/bin/env python3
"""Independent exhaustive event-timeline checks for the batch scoring engine."""

import copy
import itertools
import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import tagcalc as model
import tagcalc_engine as engine


def exhaustive(tags, fillers, baseline=(4, 2), real_max=38):
    """Enumerate every route, without DP or coordinate arithmetic.

    Reuses the supplied script's event timeline and probability formulas so
    the oracle tests the search and copy indexing independently of the engine.
    """
    events = model.build_events(tags, fillers, 1, real_max + len(fillers))
    initial = model.first_copy_index(events, baseline)
    pairs = model.make_candidate_pairs(events)
    best = (-1.0, [])

    def visit(T, r, last_shop, path):
        nonlocal best
        for pair in pairs:
            if pair["first_idx"] <= last_shop:
                continue
            k = model.copy_count_between(events, last_shop - 1, pair["first_idx"], set())
            g = model.copy_count_between(events, pair["first_idx"], pair["shop_idx"],
                                         {pair["first_idx"], pair["second_idx"]})
            F = model.copy_count_from(events, pair["shop_idx"], set())
            score = T * model.final_redeem_multiplier(r, k, g) * F
            step = (pair["neg_label"], pair["rare_label"], k, g, F)
            if score > best[0]:
                best = score, path + [step]
            M = model.normal_redeem_multiplier(r, k, g)
            if M > 0:
                visit(T * M, model.next_r_after_normal_redeem(r, k, g, M),
                      pair["shop_idx"], path + [step[:-1] + (0,)])

    visit(1.0, 7.0 / 17.0, initial, [])
    return best


class TagcalcEngineTests(unittest.TestCase):
    def assertOracle(self, text, fillers, baseline=(4, 2), real_max=38):
        tags = model.parse_tags(text)
        expected = exhaustive(tags, fillers, baseline, real_max)
        actual = engine.optimize_for_fillers(tags, fillers, baseline, 1, real_max)
        self.assertAlmostEqual(actual[0], expected[0], places=10)
        if expected[1]:
            self.assertEqual(sum(bool(step["final"]) for step in actual[1]), 1)
            self.assertTrue(actual[1][-1]["final"])
        return actual

    def test_first_copy_includes_previous_shop_and_small_second_redeems_big(self):
        result = self.assertOracle("n5b r7s", (), (4, 2))
        step = result[1][0]
        self.assertEqual((step["k"], step["g"], step["F"]), (2, 4, 95))
        self.assertEqual(step["redeem_shop_desc"], "A7 Big shop exit")

    def test_big_second_redeems_same_ante_boss(self):
        result = self.assertOracle("n5s r7b", (), (4, 2))
        step = result[1][0]
        self.assertEqual((step["k"], step["g"], step["F"]), (1, 6, 94))
        self.assertEqual(step["redeem_shop_desc"], "A7 Boss shop exit")

    def test_same_ante_pair_and_adjacent_redeem_shop_cannot_be_skipped(self):
        self.assertOracle("n5s r5b n6s r6b n7s r7b", (5, 5))
        self.assertOracle("n5b r7s n7b r10s", (39, 39))

    def test_max_total_pruning_counterexample_retains_better_composition(self):
        result = self.assertOracle(
            "r7s n15b r19s r20s r22s n23b n25s n26s r27b n29s r36b r37s", (8, 28))
        self.assertAlmostEqual(result[0], 523.928333260871, places=10)
        self.assertGreater(result[0], 520.873478955797)
        self.assertEqual([(step["neg_label"], step["rare_label"]) for step in result[1]], [
            ("NEG A15BB", "RARE A19SB"),
            ("NEG A23BB", "RARE A22SB"),
            ("NEG A29SB", "RARE A27BB"),
        ])

    def test_random_routes_match_unpruned_event_oracle(self):
        rng = random.Random(205)
        for case in range(35):
            slots = sorted(rng.sample(range(76), rng.randrange(2, 9)))
            tags = [dict(kind=rng.choice(("neg", "rare")), real_ante=slot // 2 + 1,
                         blind=slot % 2, label="tag-%d" % slot) for slot in slots]
            fillers = tuple(sorted(rng.choices(range(5, 40), k=2)))
            with self.subTest(case=case, fillers=fillers):
                expected = exhaustive(tags, fillers)
                actual = engine.optimize_for_fillers(tags, fillers, (4, 2))
                self.assertAlmostEqual(actual[0], expected[0], places=10)

    def test_equivalent_filler_gaps_match_all_630_placements(self):
        for text in ("n7s r10b n19s r28b", "r5s n5b r6s n38b", "n1s r3b n36s r38b"):
            tags = model.parse_tags(text)
            best = (-1.0, None)
            for fillers in itertools.combinations_with_replacement(range(5, 40), 2):
                score, _ = exhaustive(tags, fillers)
                if score > best[0]:
                    best = score, fillers
            result = engine.optimize(tags, (4, 2))
            self.assertAlmostEqual(result[0], best[0], places=10)
            self.assertEqual(result[2], best[1])

    def test_gap_reduction_matches_all_fixed_filler_searches_with_many_tags(self):
        tags = model.parse_tags("n5s r7b n9s r11b n14s r17b n20s r23b n26s r29b n32s r36b")
        full = max((engine.optimize_for_fillers(tags, fillers, (4, 2))
                    for fillers in itertools.combinations_with_replacement(range(5, 40), 2)),
                   key=lambda result: result[0])
        result = engine.optimize(tags, (4, 2))
        self.assertEqual(result, full)

    def test_no_valid_redeem_preserves_no_route_result(self):
        for text in ("", "n8s n9s", "n1s r3b"):
            self.assertEqual(engine.optimize(model.parse_tags(text), (4, 2)), (-1.0, [], (5, 5)))

    def test_duplicate_physical_tags_do_not_create_extra_redeems(self):
        tags = model.parse_tags("n5s r7b n12s r16b")
        original = copy.deepcopy(tags)
        expected = engine.optimize(tags, (4, 2))
        self.assertEqual(engine.optimize(list(reversed(tags)) + tags, (4, 2)), expected)
        self.assertEqual(tags, original)
        # Returned dictionaries belong to the call, so batch consumers may annotate them.
        expected[1][0]["k"] = -999
        self.assertGreaterEqual(engine.optimize(tags, (4, 2))[1][0]["k"], 1)

    def test_ante_39_is_ignored_and_conflicting_physical_placement_rejected(self):
        tags = model.parse_tags("n5s r7b")
        self.assertEqual(engine.optimize(tags + model.parse_tags("n39s r39b"), (4, 2)),
                         engine.optimize(tags, (4, 2)))
        with self.assertRaisesRegex(ValueError, "cannot be both"):
            engine.optimize(model.parse_tags("n5s r5s"), (4, 2))

    def test_invalid_antes_blinds_kinds_and_baselines_fail(self):
        for field, value in (("real_ante", 0), ("real_ante", 40), ("real_ante", True),
                             ("blind", 2), ("blind", False), ("kind", "other")):
            tag = dict(real_ante=5, blind=0, kind="neg")
            tag[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                engine.optimize([tag], (4, 2))
        for baseline in ((0, 2), (4, 3), (41, 2), (True, 0), (4,)):
            with self.subTest(baseline=baseline), self.assertRaises(ValueError):
                engine.optimize([], baseline)


if __name__ == "__main__":
    unittest.main()
