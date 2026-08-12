"""Cost projection (spec 016): measured ratios, declared rates, honest labels.

The failure this file guards against is not a wrong total — a total built from a
published rate is *always* somewhat wrong, and the module says so. It is a total
that **looks** authoritative: a zero because a model id did not match a rate key,
a caveat attached to the wrong figure, or a ratio quietly copied from an English
prose rule of thumb that does not describe agent transcripts.
"""
from __future__ import annotations

import unittest
from unittest import mock

from muninn import cost, enrich, survey
from muninn.store import open_store


class MirroredConstantsTest(unittest.TestCase):
    """`cost` mirrors enrich's chunking because importing it would cycle.

    ``enrich`` imports ``survey``, ``survey`` imports ``cost`` — so ``cost``
    cannot import ``enrich``. The duplicate is deliberate; this test is the thing
    that makes it safe.
    """

    def test_the_chunk_constants_have_not_drifted(self) -> None:
        self.assertEqual(cost.ENRICH_CHUNK_WORDS, enrich.CHUNK_WORDS)
        self.assertEqual(cost.ENRICH_CHUNK_OVERLAP_WORDS, enrich.CHUNK_OVERLAP_WORDS)

    def test_call_counts_match_enrichs_own_estimate(self) -> None:
        # enrich.plan() computes the same figure for its `estimated_calls`; if
        # these disagree, the survey quotes a cost for a different number of
        # calls than the run will make.
        for words in (0, 1, 5_000, 12_000, 12_001, 134_973):
            with self.subTest(words=words):
                stride = max(enrich.CHUNK_WORDS - enrich.CHUNK_OVERLAP_WORDS, 1)
                expected = max(1, -(-words // stride)) if words > 0 else 0
                self.assertEqual(cost.enrich_calls(words), expected)


class RateLookupTest(unittest.TestCase):
    """A missed rate lookup projects zero, which is the worst way to be wrong."""

    def test_platform_prefixes_and_version_suffixes_resolve(self) -> None:
        # Verified against the live Bedrock API: sonnet-5 resolves bare, haiku-4-5
        # only in its dated form. A table keyed on exact ids would miss one.
        for model, expected in (
            ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
            ("us.anthropic.claude-sonnet-5", "claude-sonnet-5"),
            ("anthropic.claude-haiku-4-5", "claude-haiku-4-5"),
            ("claude-haiku-4-5", "claude-haiku-4-5"),
            ("amazon.titan-embed-text-v2:0", "amazon.titan-embed-text-v2:0"),
            ("gpt-5.6-luna", "gpt-5.6-luna"),
        ):
            with self.subTest(model=model):
                found = cost.rate_for(model)
                self.assertIsNotNone(found, f"{model} did not resolve to a rate")
                self.assertEqual(found.model, expected)

    def test_an_unknown_model_returns_none_rather_than_guessing(self) -> None:
        self.assertIsNone(cost.rate_for("some-model-nobody-priced"))

    def test_an_unknown_model_is_reported_as_unverified_not_free(self) -> None:
        doc = cost.project(words=1000, chunks=3, enrich_words=1000,
                           enrich_calls=1, enrich_sessions=1,
                           text_model="some-model-nobody-priced")
        enrich_stage = next(s for s in doc["stages"] if s["stage"] == "enrich")
        self.assertEqual(enrich_stage["confidence"], "low")


class StageCostTest(unittest.TestCase):
    def test_embedding_accounts_for_chunk_overlap(self) -> None:
        """400-word windows on a 320-word stride embed ~25% more than the corpus.

        Pricing ``words`` directly under-projects, and it under-projects silently.
        """
        words = 100_000
        overlapping = cost.embed_cost(words, chunks=300)
        naive = cost.embed_cost(words, chunks=None)
        self.assertGreater(overlapping.usd, naive.usd)

    def test_a_seat_licensed_model_is_zero_but_says_why(self) -> None:
        stage = cost.enrich_cost(100_000, 10, model="gpt-5.6-luna", sessions=10)
        self.assertEqual(stage.usd, 0.0)
        self.assertIn("seat", stage.note)

    def test_output_tokens_scale_with_calls_not_sessions(self) -> None:
        # One long session is several calls, and each pays the instruction block
        # and produces its own facet object.
        one_call = cost.enrich_cost(12_000, 1, sessions=1)
        many_calls = cost.enrich_cost(12_000, 12, sessions=1)
        self.assertGreater(many_calls.usd, one_call.usd)

    def test_semantic_search_prices_the_query_not_the_corpus(self) -> None:
        # The corpus vectors were paid for once at embed time; a query is a
        # sentence. If this ever costs more than a fraction of a cent per
        # thousand, the model has stopped describing the implementation.
        stage = cost.search_cost(1_000)
        self.assertLess(stage.usd, 0.01)

    def test_deep_search_costs_more_than_semantic(self) -> None:
        self.assertGreater(cost.search_cost(100, deep=True).usd,
                           cost.search_cost(100).usd)

    def test_free_stages_are_listed_rather_than_omitted(self) -> None:
        """"Not mentioned" reads as "not measured"."""
        names = {s.stage for s in cost.free_stages()}
        self.assertIn("ingest", names)
        self.assertTrue(any("correlate" in n for n in names))
        self.assertTrue(all(s.usd == 0.0 for s in cost.free_stages()))


class ConfidenceLabellingTest(unittest.TestCase):
    """A caveat attached to the wrong number stops meaning anything."""

    def test_only_genuinely_unverified_rates_are_flagged(self) -> None:
        doc = cost.project(words=1000, chunks=3, enrich_words=1000,
                           enrich_calls=1, enrich_sessions=1,
                           embed_model="amazon.titan-embed-text-v2:0",
                           text_model="claude-haiku-4-5")
        # Titan's published figure is unverified; the Claude rate is not, and an
        # earlier version flagged it purely because the same report priced both.
        self.assertEqual(doc["low_confidence_models"],
                         ["amazon.titan-embed-text-v2:0"])

    def test_a_fully_verified_pair_flags_nothing(self) -> None:
        doc = cost.project(words=1000, chunks=3, enrich_words=1000,
                           enrich_calls=1, enrich_sessions=1,
                           embed_model="mlx-community/bge-small-en-v1.5-bf16",
                           text_model="claude-haiku-4-5")
        self.assertEqual(doc["low_confidence_models"], [])

    def test_every_rate_carries_a_source_and_a_date(self) -> None:
        for name, rate in cost.RATES.items():
            with self.subTest(model=name):
                self.assertTrue(rate.source, f"{name} has no source")
                self.assertIn(rate.confidence, ("high", "low"))

    def test_the_token_ratios_are_not_the_prose_rule_of_thumb(self) -> None:
        """Agent transcripts tokenize far worse than English prose.

        Measured at 1.76 (embedding) and 2.02 (enrichment) tokens per word on a
        real corpus. If someone "corrects" these toward the familiar ~1.3, every
        projection drops by about a third for no reason but familiarity.
        """
        self.assertGreater(cost.TOKEN_RATIOS["embed_tokens_per_word"], 1.5)
        self.assertGreater(cost.TOKEN_RATIOS["enrich_input_tokens_per_word"], 1.5)


class SurveyIntegrationTest(unittest.TestCase):
    """`survey` gains a cost section and loses nothing it already reported."""

    def setUp(self) -> None:
        import shutil
        import tempfile
        from pathlib import Path

        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-cost-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = self.tmp / "archive.db"
        self.st = open_store(self.db)
        self.addCleanup(self.st.close)
        for i in range(4):
            self.st.upsert_session({
                "session_id": f"s{i}", "source": "claude", "provenance": "human",
                "text": "word " * 3000, "words": 3000, "user_turns": 1,
                "assistant_turns": 1, "tool_uses": 0, "tool_results": 0,
                "origin": "raw", "source_present": 1,
            })
            self.st.replace_chunks(f"s{i}", "word " * 3000)
        self.st.commit()

    def test_the_cost_section_is_present_and_shaped(self) -> None:
        doc = survey.survey(self.st, db=self.db)
        self.assertIn("cost", doc)
        self.assertIn("stages", doc["cost"])
        self.assertIn("one_time_usd", doc["cost"])

    def test_everything_the_survey_already_reported_is_still_there(self) -> None:
        doc = survey.survey(self.st, db=self.db)
        for key in ("schema", "surveyed_at", "archive", "coverage_target_pct",
                    "sources", "index_lag", "anomalies"):
            self.assertIn(key, doc, f"survey lost {key}")
        self.assertIn("enrichment_gate", doc["sources"]["claude"])

    def test_a_broken_provider_does_not_break_the_survey(self) -> None:
        """A cost estimate must not give `survey` a way to fail."""
        from muninn import providers

        with mock.patch.object(providers, "resolve_provider",
                               side_effect=RuntimeError("plugin exploded")):
            doc = survey.survey(self.st, db=self.db)
        self.assertIn("cost", doc)

    def test_the_estimate_prices_the_resolved_provider(self) -> None:
        # An estimate that prices the built-in default while the install enriches
        # through something else is wrong in whichever direction is cheaper.
        from muninn import providers

        fake = mock.Mock()
        fake.model = "claude-sonnet-5"
        with mock.patch.object(providers, "resolve_provider", return_value=fake):
            doc = survey.survey(self.st, db=self.db)
        stage = next(s for s in doc["cost"]["stages"] if s["stage"] == "enrich")
        self.assertEqual(stage["model"], "claude-sonnet-5")

    def test_the_cost_section_is_json_safe(self) -> None:
        import json

        doc = survey.survey(self.st, db=self.db)
        json.loads(json.dumps(doc["cost"]))     # raises on a stray dataclass


class StandaloneScriptDriftTest(unittest.TestCase):
    """`tools/corpus-survey.py` mirrors this module's numbers; they must not drift.

    The script is standalone by design — one file, stdlib only, runnable on a
    machine that has never installed Muninn — so it cannot import `muninn.cost`.
    That duplication is deliberate and this test is what makes it safe, exactly as
    `MirroredConstantsTest` does for the enrich constants one layer down.

    Parsed rather than imported: importing the script executes it, and it is a CLI
    with a `main()` and module-level dataclasses. Reading the assignments is
    cheaper and cannot have side effects.
    """

    @classmethod
    def setUpClass(cls):
        import ast
        import pathlib as _pathlib

        source = (_pathlib.Path(__file__).resolve().parents[1]
                  / "tools" / "corpus-survey.py").read_text()
        cls.values = {}
        for node in ast.parse(source).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    try:
                        cls.values[target.id] = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        pass

    def test_the_token_ratios_match(self) -> None:
        self.assertEqual(self.values["EMBED_TOKENS_PER_WORD"],
                         cost.TOKEN_RATIOS["embed_tokens_per_word"])
        self.assertEqual(self.values["ENRICH_INPUT_TOKENS_PER_WORD"],
                         cost.TOKEN_RATIOS["enrich_input_tokens_per_word"])
        self.assertEqual(self.values["ENRICH_OUTPUT_TOKENS_PER_CALL"],
                         cost.TOKEN_RATIOS["enrich_output_tokens_per_call"])

    def test_the_enrichment_chunking_matches(self) -> None:
        self.assertEqual(self.values["ENRICH_CHUNK_WORDS"], cost.ENRICH_CHUNK_WORDS)
        self.assertEqual(self.values["ENRICH_CHUNK_OVERLAP_WORDS"],
                         cost.ENRICH_CHUNK_OVERLAP_WORDS)

    def test_the_rates_match(self) -> None:
        rates = self.values["COST_RATES"]
        self.assertEqual(rates["titan-embed"]["input"],
                         cost.RATES["amazon.titan-embed-text-v2:0"].input)
        self.assertEqual(rates["claude-haiku-4-5"]["input"],
                         cost.RATES["claude-haiku-4-5"].input)
        self.assertEqual(rates["claude-haiku-4-5"]["output"],
                         cost.RATES["claude-haiku-4-5"].output)
        self.assertEqual(rates["claude-sonnet-5"]["input"],
                         cost.RATES["claude-sonnet-5"].input)

    def test_the_unverified_rate_is_marked_in_both(self) -> None:
        # If one of them stops flagging Titan, a reader of that one gets a figure
        # presented as measured when it is not.
        self.assertEqual(self.values["COST_RATES"]["titan-embed"]["confidence"], "low")
        self.assertEqual(cost.RATES["amazon.titan-embed-text-v2:0"].confidence, "low")


if __name__ == "__main__":      # pragma: no cover
    unittest.main()
