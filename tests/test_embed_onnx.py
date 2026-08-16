"""The cross-platform embedding provider.

Semantic search was Mac-only before this: MLX is Apple-silicon only, so on
Windows and Linux the default build resolved no provider at all — and the
[semantic] extra it told you to install contained nothing but numpy off Apple
silicon, so following the advice changed nothing.
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

from muninn import embed
from muninn.embed_onnx import DEFAULT_DIM, DEFAULT_MODEL, ONNXEmbeddingProvider


def _runtime_installed() -> bool:
    return ONNXEmbeddingProvider().available() is None


class ProviderContractTest(unittest.TestCase):
    def test_available_does_no_io(self):
        """Discovery runs this; a probe that touches the network is a hang."""
        provider = ONNXEmbeddingProvider()
        with patch("huggingface_hub.hf_hub_download") as download:
            provider.available()
        download.assert_not_called()

    def test_a_missing_runtime_is_reported_not_raised(self):
        provider = ONNXEmbeddingProvider()
        with patch.dict(sys.modules, {"onnxruntime": None}):
            reason = provider.available()
        self.assertIsNotNone(reason)
        self.assertIn("semantic", reason)

    def test_its_model_id_is_its_own(self):
        """Not the MLX id. The bf16 and fp32 vectors are not the same space.

        chunk_vectors keys on the model id so two spaces are never compared;
        reusing MLX's id to avoid a re-embed would silently mix them.
        """
        self.assertEqual(DEFAULT_MODEL, "BAAI/bge-small-en-v1.5")
        self.assertNotIn("mlx", DEFAULT_MODEL)
        self.assertEqual(DEFAULT_DIM, 384)

    def test_the_policy_chokepoint_is_not_bypassed(self):
        """An embedding call is still a model call."""
        provider = ONNXEmbeddingProvider()
        with patch("muninn.policy.check", side_effect=RuntimeError("checked")) as check:
            with self.assertRaises(RuntimeError):
                provider.embed(["anything"])
        check.assert_called_once_with(DEFAULT_MODEL, provider.name)


@unittest.skipUnless(_runtime_installed(), "onnxruntime not installed")
class ProviderBehaviourTest(unittest.TestCase):
    """Real inference. Skipped where the optional runtime is absent."""

    @classmethod
    def setUpClass(cls):
        cls.provider = ONNXEmbeddingProvider()

    def test_vectors_are_normalised_float32_of_the_declared_width(self):
        import numpy as np

        out = self.provider.embed(["one", "two"])
        self.assertEqual(out.shape, (2, DEFAULT_DIM))
        self.assertEqual(out.dtype, np.float32)
        # Search treats cosine as a dot product and relies on this.
        self.assertTrue(np.allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-5))

    def test_related_text_scores_above_unrelated(self):
        out = self.provider.embed([
            "redscript compilation failure in Cyberpunk 2077",
            "Cyberpunk mod compiler errors and how to repair them",
            "a cat asleep on a sunny windowsill",
        ])
        related = float(out[0] @ out[1])
        unrelated = float(out[0] @ out[2])
        self.assertGreater(related, unrelated + 0.2)

    def test_an_empty_batch_is_an_empty_array_not_an_error(self):
        self.assertEqual(self.provider.embed([]).shape, (0, DEFAULT_DIM))

    def test_batching_does_not_change_the_vectors(self):
        """Rows are padded per batch, so batching must not shift results."""
        import numpy as np

        texts = [f"chunk number {i} about deployment and containers" for i in range(5)]
        with patch("muninn.embed_onnx.BATCH", 2):
            batched = self.provider.embed(texts)
        with patch("muninn.embed_onnx.BATCH", 64):
            single = self.provider.embed(texts)
        self.assertTrue(np.allclose(batched, single, atol=1e-4))

    def test_text_beyond_the_context_limit_truncates_rather_than_raising(self):
        out = self.provider.embed(["word " * 5000])
        self.assertEqual(out.shape, (1, DEFAULT_DIM))


class ResolutionTest(unittest.TestCase):
    def test_onnx_is_resolved_when_mlx_cannot_run(self):
        """The Windows and Linux path: MLX unavailable, ONNX answers."""
        mlx = MagicMock()
        mlx.available.return_value = "mlx is Apple-silicon only; this is win32"
        onnx = MagicMock()
        onnx.available.return_value = None

        with patch("muninn.embed_mlx.MLXEmbeddingProvider", return_value=mlx), \
             patch("muninn.embed_onnx.ONNXEmbeddingProvider", return_value=onnx), \
             patch("muninn.plugins.discover_plugins", return_value=MagicMock(specs=[])):
            self.assertIs(embed.resolve_provider(), onnx)

    def test_mlx_keeps_priority_where_it_works(self):
        mlx = MagicMock()
        mlx.available.return_value = None
        onnx = MagicMock()
        onnx.available.return_value = None

        with patch("muninn.embed_mlx.MLXEmbeddingProvider", return_value=mlx), \
             patch("muninn.embed_onnx.ONNXEmbeddingProvider", return_value=onnx), \
             patch("muninn.plugins.discover_plugins", return_value=MagicMock(specs=[])):
            self.assertIs(embed.resolve_provider(), mlx)

    def test_every_reason_is_reported_when_none_can_run(self):
        mlx = MagicMock()
        mlx.available.return_value = "mlx is Apple-silicon only; this is win32"
        onnx = MagicMock()
        onnx.available.return_value = "onnxruntime is not installed"

        with patch("muninn.embed_mlx.MLXEmbeddingProvider", return_value=mlx), \
             patch("muninn.embed_onnx.ONNXEmbeddingProvider", return_value=onnx), \
             patch("muninn.plugins.discover_plugins", return_value=MagicMock(specs=[])):
            with self.assertRaises(embed.EmbeddingUnavailable) as caught:
                embed.resolve_provider()
        message = str(caught.exception)
        self.assertIn("Apple-silicon", message)
        self.assertIn("onnxruntime", message)


if __name__ == "__main__":
    unittest.main()
