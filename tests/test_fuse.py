"""Reciprocal-rank fusion, checked against hand-computed scores.

Every assertion here is arithmetic someone can redo on paper, because this is
the rule that decides what a person sees first and "it looked about right" is
not a test of a ranking function.
"""
from __future__ import annotations

import unittest

from muninn import fuse


class RRFTest(unittest.TestCase):
    def test_scores_match_the_formula(self) -> None:
        # k=60, rank is 1-based: a = 1/61 + 1/62, b = 1/62 + 1/61 ... so use
        # distinct ranks to make the arithmetic visible.
        scored = dict(fuse.rrf([["a", "b"], ["b", "a"]]))
        self.assertAlmostEqual(scored["a"], 1 / 61 + 1 / 62)
        self.assertAlmostEqual(scored["b"], 1 / 62 + 1 / 61)

    def test_first_and_third_beats_second_and_second(self) -> None:
        # Criterion 1, and the property that makes RRF worth using: a result one
        # engine is sure about outranks one both engines are lukewarm on.
        #   x is 1st then 3rd:  1/61 + 1/63 = 0.032266...
        #   y is 3rd then 2nd:  1/63 + 1/62 = 0.032002...
        scored = dict(fuse.rrf([["x", "w", "y"], ["w", "y", "x"]]))
        self.assertAlmostEqual(scored["x"], 1 / 61 + 1 / 63)
        self.assertAlmostEqual(scored["y"], 1 / 63 + 1 / 62)
        self.assertGreater(scored["x"], scored["y"])

    def test_disjoint_lists_keep_everything(self) -> None:
        # Criterion 2. Fusion is a union, not an intersection — the entire point
        # is that lexical and semantic search find different things.
        fused = fuse.fuse_ids([["a", "b"], ["c", "d"]])
        self.assertEqual(sorted(fused), ["a", "b", "c", "d"])
        self.assertEqual(fused[0], "a")   # rank 1 of its list beats rank 2 of the other

    def test_a_single_list_is_passed_through_in_order(self) -> None:
        self.assertEqual(fuse.fuse_ids([["a", "b", "c"]]), ["a", "b", "c"])

    def test_no_lists_and_empty_lists_are_empty(self) -> None:
        self.assertEqual(fuse.fuse_ids([]), [])
        self.assertEqual(fuse.fuse_ids([[], []]), [])

    def test_ties_break_deterministically(self) -> None:
        # Two results with identical scores are indistinguishable to RRF, and
        # letting dict order decide would make one query return two orders.
        first = fuse.fuse_ids([["b", "a"], ["a", "b"]])
        self.assertEqual(first, fuse.fuse_ids([["b", "a"], ["a", "b"]]))
        self.assertEqual(first, sorted(first))

    def test_limit_truncates_after_fusing_not_before(self) -> None:
        # Truncating the inputs first would drop a result ranked low in both
        # lists that fusion would have promoted.
        self.assertEqual(fuse.fuse_ids([["a", "b", "c"], ["c", "b", "a"]], limit=2),
                         ["a", "c"])

    def test_a_smaller_k_sharpens_the_top_rank(self) -> None:
        # Documents the knob's meaning rather than pinning the default: with
        # k=1, rank 1 dominates and fusion becomes "whichever engine was more
        # confident" — which is why the default is 60.
        sharp = dict(fuse.rrf([["x", "w", "y"], ["w", "y", "x"]], k=1))
        self.assertGreater(sharp["x"], sharp["y"])
        self.assertAlmostEqual(sharp["x"], 1 / 2 + 1 / 4)

    def test_the_default_k_is_sixty(self) -> None:
        self.assertEqual(fuse.DEFAULT_K, 60)


if __name__ == "__main__":
    unittest.main()
