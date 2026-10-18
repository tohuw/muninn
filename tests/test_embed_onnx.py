"""The cross-platform embedding provider.

Semantic search was Mac-only before this: MLX is Apple-silicon only, so on
Windows and Linux the default build resolved no provider at all — and the
[semantic] extra it told you to install contained nothing but numpy off Apple
silicon, so following the advice changed nothing.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from muninn import embed
from muninn.embed_onnx import (BATCH, DEFAULT_DIM, DEFAULT_MODEL, MAX_TOKENS,
                               ONNX_FILE, TOKENIZER_FILE,
                               ONNXEmbeddingProvider)


def _runtime_installed() -> bool:
    return ONNXEmbeddingProvider().available() is None


def _weights_cached() -> bool:
    """True only if the model is already on disk. A test must not download it."""
    if not _runtime_installed():
        return False
    try:
        from huggingface_hub import hf_hub_download

        for filename in (ONNX_FILE, TOKENIZER_FILE):
            hf_hub_download(DEFAULT_MODEL, filename, local_files_only=True)
    except Exception:
        return False
    return True


def _resident_bytes() -> int | None:
    """Current resident set size, or ``None`` where it cannot be read.

    Current rather than peak: the arena's signature is memory still held once
    the pass is over, and peak also counts transient allocations that a
    correctly configured run frees immediately.
    """
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes as wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t),
                        ("PrivateUsage", ctypes.c_size_t)]

        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        if not psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return None
        return int(counters.WorkingSetSize)

    try:
        import resource

        with open("/proc/self/statm", encoding="ascii") as handle:
            return int(handle.read().split()[1]) * resource.getpagesize()
    except OSError:
        pass

    try:
        import subprocess

        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                             capture_output=True, text=True, check=True)
        return int(out.stdout.strip()) * 1024      # ps reports KiB
    except Exception:
        return None


class ProviderContractTest(unittest.TestCase):
    def test_available_does_no_io(self):
        """Discovery runs this; a probe that touches the network is a hang.

        The hub module is injected rather than patched in place, so this runs
        without the [semantic] extra too -- patching a module that is not
        installed is an error, and CI does not install it. What is asserted is
        that available() never calls the downloader, which is as true of a
        stand-in as of the real one.
        """
        provider = ONNXEmbeddingProvider()
        download = MagicMock()
        with patch.dict(sys.modules,
                        {"huggingface_hub": MagicMock(hf_hub_download=download)}):
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
        """An embedding call is still a model call.

        available() is stubbed so the chokepoint is asserted on machines
        without the extra as well. Otherwise embed() refuses at the
        availability gate before reaching policy, and the security property
        this defends would go unchecked exactly where it is least observed --
        including CI. That check raises and the weights are never loaded is
        also the ordering assertion: policy comes first.
        """
        provider = ONNXEmbeddingProvider()
        with patch.object(provider, "available", return_value=None), \
             patch("muninn.policy.check", side_effect=RuntimeError("checked")) as check:
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


class FootprintTest(unittest.TestCase):
    """The daemon hosts this provider for weeks. Peak memory is the footprint.

    `muninn serve` was found holding 1.5 GB resident with an empty embedding
    backlog, days after the last vector was written. Nothing was leaking: ONNX
    Runtime's arena allocator had honoured the high-water mark of a single
    forward pass and, by design, never gave it back. With ``BATCH`` at 32 and
    85% of real chunks padding to the 512-token cap, that mark was 2.1 GB.

    Both halves of the fix are asserted, and the arithmetic ones run
    everywhere, because the machine where this regresses is the one that
    cannot run the measurement: the default build installs no provider at all,
    so a skip-only guard would wave a revert through CI.
    """

    def test_the_arena_is_disabled(self):
        """Without this the process keeps the peak of its largest batch forever.

        Asserted through injected modules rather than a real session so it
        holds without the [semantic] extra too. What matters is the value set
        on the options object handed to ``InferenceSession``, and a stand-in
        records that as faithfully as the real runtime.
        """
        options = MagicMock()
        ort = MagicMock()
        ort.SessionOptions.return_value = options
        session = MagicMock()
        session.get_inputs.return_value = []
        ort.InferenceSession.return_value = session

        with patch.dict(sys.modules, {
            "onnxruntime": ort,
            "huggingface_hub": MagicMock(hf_hub_download=MagicMock(return_value="x")),
            "tokenizers": MagicMock(),
        }):
            ONNXEmbeddingProvider()._ensure_loaded()

        # assertIs, not assertFalse: an attribute nobody set is a MagicMock,
        # which is truthy here but would still satisfy a laxer assertion if
        # the sense of the flag were ever inverted.
        self.assertIs(options.enable_cpu_mem_arena, False)
        ort.InferenceSession.assert_called_once()
        self.assertIs(ort.InferenceSession.call_args.args[1], options)

    def test_the_batch_cannot_grow_back_into_gigabyte_attention_tensors(self):
        """``BATCH`` alone sizes the attention tensors, and they are quadratic.

        85% of real chunks reach ``MAX_TOKENS``, so the padded length is a
        constant in practice and the batch is the only free variable. BERT's
        attention scores are ``batch x heads x seq x seq`` floats with several
        live at once: 100 MB at 8, 400 MB at 32. Arithmetic rather than a
        measurement on purpose — it fails on a machine that cannot load the
        weights, and it names the quantity that made 32 the wrong number.
        """
        heads, float_bytes = 12, 4
        score_tensor = BATCH * heads * MAX_TOKENS * MAX_TOKENS * float_bytes
        self.assertLess(score_tensor, 192 * 2 ** 20,
                        f"BATCH={BATCH} puts one attention tensor at "
                        f"{score_tensor / 2 ** 20:.0f} MB")

    @unittest.skipUnless(_weights_cached(), "model weights are not cached locally")
    def test_a_full_pass_does_not_raise_the_resident_footprint(self):
        """The measurement the comments cite, as a test rather than a claim.

        Growth over a post-load baseline is asserted rather than an absolute
        ceiling: the floor is the weights plus numpy and onnxruntime and moves
        with the platform, while the regression is unmistakable in the delta —
        10 MB as written, 1,895 MB with the arena back on.

        The rows are padded to the cap deliberately. A batch of short strings
        allocates almost nothing and passes with the arena enabled, which is
        how this went unnoticed for as long as it did.
        """
        if _resident_bytes() is None:
            self.skipTest("no resident-memory reading on this platform")

        # Its own provider, so the session — and therefore any arena — is
        # created inside the window being measured.
        provider = ONNXEmbeddingProvider()
        provider._ensure_loaded()
        baseline = _resident_bytes()

        at_cap = ("the archive is a system of record for data that no longer "
                  "exists anywhere else " * 60)
        provider.embed([at_cap] * (BATCH * 8))

        growth = _resident_bytes() - baseline
        self.assertLess(growth, 256 * 2 ** 20,
                        f"one pass added {growth / 2 ** 20:.0f} MB of resident "
                        f"memory; the daemon never gives that back")


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
