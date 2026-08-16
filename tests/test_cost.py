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


class RatesFileTestCase(unittest.TestCase):
    """Base for anything that needs prices, since none are shipped.

    ``load_rates`` merges into the module-global ``RATES``, so every test that
    calls it has to put the global back or it leaks into the next one — which is
    exactly how a "no prices ship" guarantee would quietly stop being true
    inside the suite that checks it.
    """

    RATES = {
        "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "as_of": "2026-06-24",
                             "source": "vendor pricing page, read by hand"},
        "some-embedder": {"input": 0.02, "output": None, "as_of": "2026-08-12",
                          "confidence": "low",
                          "source": "a figure someone repeated, unverified"},
        "seat-model": {"input": 9.0, "output": 9.0, "as_of": "2026-08-12",
                       "source": "list price, but my access is seat-based",
                       "seat_licensed": True},
    }

    def setUp(self) -> None:
        import json as _json
        import shutil
        import tempfile
        from pathlib import Path
        self._snapshot = dict(cost.RATES)
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-rates-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(self._restore)
        self.db = self.tmp / "archive.db"
        (self.tmp / "rates.json").write_text(_json.dumps(self.rates_payload()),
                                             encoding="utf-8")
        cost.load_rates(self.db)

    def rates_payload(self) -> dict:
        return self.RATES

    def _restore(self) -> None:
        cost.RATES.clear()
        cost.RATES.update(self._snapshot)


class NoPricesShipTest(unittest.TestCase):
    """The guarantee itself: this repository declares no hosted model's price.

    A price in source is checked once, by one person, against one vendor's page,
    for one billing arrangement — and then renders to two decimal places on
    somebody else's machine forever. It also asserts something about the
    reader's account that no code here can observe.
    """

    def test_no_hosted_model_carries_a_price(self) -> None:
        for name, rate in cost.RATES.items():
            with self.subTest(model=name):
                self.assertTrue(
                    rate.local,
                    f"{name} ships a price; rates belong in the reader's "
                    f"rates.json, not in this repository")

    def test_local_inference_is_zero_because_of_where_it_runs(self) -> None:
        """Not a price anyone looked up — nothing leaves the box."""
        for name, rate in cost.LOCAL_RATES.items():
            with self.subTest(model=name):
                self.assertEqual(rate.input, 0.0)
                self.assertTrue(rate.local)
                self.assertFalse(rate.seat_licensed)

    def test_nothing_declares_someone_elses_billing_arrangement(self) -> None:
        """``seat_licensed`` is the reader's fact to state, never ours."""
        self.assertFalse(any(r.seat_licensed for r in cost.RATES.values()))

    def test_neither_source_file_contains_a_usd_figure(self) -> None:
        """Covers the standalone script too, which had its own copy.

        Looks for the shapes a price takes — ``"input": 1.00`` — rather than for
        any number, since token ratios and chunk sizes are numbers that belong.
        """
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parents[1]
        pattern = re.compile(r'"(?:input|output)"\s*:\s*(\d+\.\d+)')
        for relative in ("muninn/cost.py", "tools/corpus-survey.py"):
            text = (root / relative).read_text(encoding="utf-8")
            with self.subTest(file=relative):
                priced = [m for m in pattern.findall(text) if float(m) != 0.0]
                self.assertEqual(priced, [], f"{relative} has a price in it")


class RateLookupTest(RatesFileTestCase):
    """A missed rate lookup must not project zero — the worst way to be wrong."""

    def test_platform_prefixes_and_version_suffixes_resolve(self) -> None:
        """One rates.json entry serves every platform reselling the same model.

        The ids are not internally consistent across platforms — some resolve
        bare, some only in a dated form — so a table keyed on exact ids would
        miss whichever form the caller happens to hold.
        """
        for model in (
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "anthropic.claude-haiku-4-5",
            "claude-haiku-4-5",
        ):
            with self.subTest(model=model):
                found = cost.rate_for(model)
                self.assertIsNotNone(found, f"{model} did not resolve to a rate")
                self.assertEqual(found.model, "claude-haiku-4-5")

    def test_an_unknown_model_returns_none_rather_than_guessing(self) -> None:
        self.assertIsNone(cost.rate_for("some-model-nobody-priced"))

    def test_an_unknown_model_is_unpriced_rather_than_zero(self) -> None:
        """The distinction this whole shape exists for.

        The previous version substituted a zero-priced stand-in and flagged it
        only with ``confidence: low``, so an unpriced stage rendered "$0.00" and
        read as "no charge".
        """
        doc = cost.project(words=1000, chunks=3, enrich_words=1000,
                           enrich_calls=1, enrich_sessions=1,
                           text_model="some-model-nobody-priced")
        enrich_stage = next(s for s in doc["stages"] if s["stage"] == "enrich")
        self.assertIsNone(enrich_stage["usd"])
        self.assertEqual(enrich_stage["unpriced_reason"], cost.UNPRICED)
        self.assertIn("some-model-nobody-priced", doc["unpriced_models"])

    def test_one_unpriced_stage_makes_the_total_unpriced(self) -> None:
        """Summing only the priced stages understates by the unchecked part."""
        doc = cost.project(words=1000, chunks=3, enrich_words=1000,
                           enrich_calls=1, enrich_sessions=1,
                           text_model="some-model-nobody-priced")
        self.assertIsNone(doc["one_time_usd"])

    def test_a_malformed_entry_is_skipped_not_defaulted(self) -> None:
        """Defaulting a price is how a typo becomes a confident number."""
        import json as _json
        (self.tmp / "rates.json").write_text(
            _json.dumps({"broken": {"input": "not a number", "source": "x",
                                    "as_of": "2026-01-01"},
                         "no-source": {"input": 1.0, "as_of": "2026-01-01"}}),
            encoding="utf-8")
        loaded = cost.load_rates(self.db)
        self.assertEqual(loaded, {})

    def test_a_missing_rates_file_is_not_an_error(self) -> None:
        self.assertEqual(cost.load_rates(self.tmp / "nowhere" / "a.db"), {})

    def test_a_stale_rate_is_named_for_re_checking(self) -> None:
        old = cost.stale_rates("2027-01-01")
        self.assertIn("claude-haiku-4-5", [r.model for r in old])
        self.assertEqual(cost.stale_rates("2026-06-25"), [])

    def test_local_models_never_go_stale(self) -> None:
        """There is nothing to go and re-check about where inference runs."""
        names = [r.model for r in cost.stale_rates("2030-01-01")]
        for local in cost.LOCAL_RATES:
            self.assertNotIn(local, names)


class StageCostTest(RatesFileTestCase):
    def test_embedding_accounts_for_chunk_overlap(self) -> None:
        """400-word windows on a 320-word stride embed ~25% more than the corpus.

        Pricing ``words`` directly under-projects, and it under-projects silently.
        """
        words = 100_000
        overlapping = cost.embed_cost(words, model="some-embedder", chunks=300)
        naive = cost.embed_cost(words, model="some-embedder", chunks=None)
        self.assertGreater(overlapping.usd, naive.usd)

    def test_a_seat_licensed_model_is_zero_but_says_why(self) -> None:
        """Zero only because the *reader's* rates.json said their access is."""
        stage = cost.enrich_cost(100_000, 10, model="seat-model", sessions=10)
        self.assertEqual(stage.usd, 0.0)
        self.assertIn("seat-licensed", stage.note)

    def test_nothing_model_backed_is_described_as_free(self) -> None:
        """Seat access draws on a shared token pool; "free" invites treating it
        as unlimited. $0.00 is fine; the *word* is what misleads."""
        stage = cost.enrich_cost(100_000, 10, model="seat-model", sessions=10)
        self.assertNotIn("free", stage.note.lower())
        for s in cost.unmetered_stages():
            self.assertNotIn("free", (s.note or "").lower())
            self.assertNotIn("free", s.stage.lower())

    def test_local_embedding_is_zero_for_a_stated_reason(self) -> None:
        stage = cost.embed_cost(100_000, chunks=300)
        self.assertEqual(stage.usd, 0.0)
        self.assertIn("this machine", stage.note)

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
        stage = cost.search_cost(1_000, semantic_model="some-embedder")
        self.assertLess(stage.usd, 0.01)

    def test_deep_search_costs_more_than_semantic(self) -> None:
        self.assertGreater(
            cost.search_cost(100, semantic_model="some-embedder", deep=True).usd,
            cost.search_cost(100, semantic_model="some-embedder").usd)

    def test_one_unpriced_hop_makes_the_whole_search_unpriced(self) -> None:
        """Pricing the hops we happen to know reports less than the truth."""
        stage = cost.search_cost(100, semantic_model="some-embedder", deep=True,
                                 rerank_model="never-priced")
        self.assertIsNone(stage.usd)

    def test_unmetered_stages_are_listed_rather_than_omitted(self) -> None:
        """"Not mentioned" reads as "not measured"."""
        names = {s.stage for s in cost.unmetered_stages()}
        self.assertIn("ingest", names)
        self.assertTrue(any("correlate" in n for n in names))
        self.assertTrue(all(s.usd == 0.0 for s in cost.unmetered_stages()))


class ConfidenceLabellingTest(RatesFileTestCase):
    """A caveat attached to the wrong number stops meaning anything."""

    def test_only_genuinely_unverified_rates_are_flagged(self) -> None:
        doc = cost.project(words=1000, chunks=3, enrich_words=1000,
                           enrich_calls=1, enrich_sessions=1,
                           embed_model="some-embedder",
                           text_model="claude-haiku-4-5")
        # The embedder's figure is declared unverified; the text rate is not, and
        # an earlier version flagged it purely because one report priced both.
        self.assertEqual(doc["low_confidence_models"], ["some-embedder"])

    def test_a_fully_verified_pair_flags_nothing(self) -> None:
        doc = cost.project(words=1000, chunks=3, enrich_words=1000,
                           enrich_calls=1, enrich_sessions=1,
                           embed_model="mlx-community/bge-small-en-v1.5-bf16",
                           text_model="claude-haiku-4-5")
        self.assertEqual(doc["low_confidence_models"], [])

    def test_every_money_figure_is_labelled_as_list_pricing(self) -> None:
        """The reader's invoice is a different thing from a published rate."""
        doc = cost.project(words=1000, chunks=3, enrich_words=1000,
                           enrich_calls=1, enrich_sessions=1,
                           text_model="claude-haiku-4-5")
        self.assertIn("list pricing", doc["caveat"])
        self.assertIn("not a quote", doc["caveat"])

    def test_a_loaded_rate_must_carry_a_source_and_a_date(self) -> None:
        """Provenance is required on the way in, so it cannot be absent later."""
        import json as _json
        (self.tmp / "rates.json").write_text(
            _json.dumps({"m": {"input": 1.0, "output": 2.0}}), encoding="utf-8")
        self.assertEqual(cost.load_rates(self.db), {})
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

    def test_the_query_time_ratios_match(self) -> None:
        # Added when the script grew search and ongoing-monthly estimates. Without
        # this, the standalone copy can price `--deep` at a stale rerank size and
        # nothing would catch it — the script has no other test that knows the
        # library's numbers.
        self.assertEqual(self.values["QUERY_TOKENS_PER_SEARCH"],
                         cost.TOKEN_RATIOS["query_tokens_per_search"])
        self.assertEqual(self.values["RERANK_INPUT_TOKENS_PER_SEARCH"],
                         cost.TOKEN_RATIOS["rerank_input_tokens_per_search"])
        self.assertEqual(self.values["RERANK_OUTPUT_TOKENS_PER_SEARCH"],
                         cost.TOKEN_RATIOS["rerank_output_tokens_per_search"])

    def test_both_files_agree_on_what_calls_no_model(self) -> None:
        """A stage listed unmetered in one and metered in the other is the bad case."""
        listed = " ".join(self.values["UNMETERED_OPERATIONS"]).lower()
        for stage in cost.unmetered_stages():
            head = stage.stage.split()[0].lower()
            self.assertIn(head, listed,
                          f"{stage.stage} is free in muninn.cost but unlisted in the script")

    def test_the_enrichment_chunking_matches(self) -> None:
        self.assertEqual(self.values["ENRICH_CHUNK_WORDS"], cost.ENRICH_CHUNK_WORDS)
        self.assertEqual(self.values["ENRICH_CHUNK_OVERLAP_WORDS"],
                         cost.ENRICH_CHUNK_OVERLAP_WORDS)

    def test_the_script_declares_no_rates_at_all(self) -> None:
        """There is no rate table left to drift.

        The script cannot reach a pricing page — it is offline and stdlib-only —
        and it runs on machines whose billing arrangements it cannot see. So it
        reports token volumes and no money, and the two files agree by having
        nothing to disagree about.
        """
        self.assertNotIn("COST_RATES", self.values)

    def test_the_script_reports_volumes_where_it_used_to_report_money(self) -> None:
        """Dropping the scenarios must not drop the measurement with them."""
        import pathlib
        source = (pathlib.Path(__file__).resolve().parents[1]
                  / "tools" / "corpus-survey.py").read_text(encoding="utf-8")
        for key in ("embed_tokens", "enrich_input_tokens", "enrich_output_tokens",
                    "estimated_enrichment_calls"):
            self.assertIn(key, source)


if __name__ == "__main__":      # pragma: no cover
    unittest.main()
