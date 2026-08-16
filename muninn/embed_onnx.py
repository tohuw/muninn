"""The cross-platform local embedding provider. Behind the ``[semantic]`` extra.

The companion to :mod:`embed_mlx`, and the reason semantic search is not a
Mac-only feature. MLX is Apple-silicon only — a real limitation, not a
temporary one — so on Windows and Linux the default build had no embedding
provider at all, and ``--semantic`` and ``correlate`` simply could not run. The
advice it printed ("install the local one with ``uv sync --extra semantic``")
was worse than nothing there: that extra's only runtime was gated to
``Darwin/arm64``, so following it installed no provider and changed nothing.

ONNX Runtime is the answer because it is genuinely portable: prebuilt CPU
wheels on every platform this project runs on, no compiler, no GPU, no torch.
The weights are the same family the MLX provider uses.

**The model id is deliberately its own.** ``BAAI/bge-small-en-v1.5`` and
``mlx-community/bge-small-en-v1.5-bf16`` are the same architecture, but the MLX
one is bf16-quantised and these vectors are fp32; they are near-identical and
not identical. ``chunk_vectors`` keys on the model id precisely so two spaces
are never mixed, and claiming equivalence here to save a re-embed would be the
exact "confident nonsense" that key exists to prevent. A machine that switches
platforms re-embeds, and that is the honest cost.

**No weights are downloaded by ``available()``.** It answers from imports
alone. The first ``embed()`` call fetches the model — see :mod:`embed_mlx` for
why a probe that quietly pulls hundreds of megabytes is a hang, not a check.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import policy

# Both set at *module* scope, before huggingface_hub is imported anywhere:
# it reads them once at import and ignores later changes. The progress bar and
# the Windows symlink warning both go to stderr, which keeps ``--json`` on
# stdout parseable, but they are noise printed on every search for a fetch that
# is a no-op once cached. Safe here because this module is imported lazily and
# only when it is about to be used.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

#: Part of the *identity* of the vectors, not a setting — it goes into the
#: primary key and two models' vectors are not comparable, so changing it is a
#: re-embed rather than a preference.
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIM = 384

#: Files fetched from the model repo. The ONNX export lives under ``onnx/``.
ONNX_FILE = "onnx/model.onnx"
TOKENIZER_FILE = "tokenizer.json"

#: BERT-family context limit. Chunks are far shorter than this; truncation is
#: here so a pathological chunk cannot raise instead of embedding.
MAX_TOKENS = 512

#: Rows per forward pass. Padding is to the longest row *in the batch*, so one
#: long chunk in a large batch inflates every row in it. This bounds that, and
#: bounds peak memory, at no measurable throughput cost for chunk-sized text.
BATCH = 32

PROVIDER_NAME = "onnx-local"


@dataclass
class ONNXEmbeddingProvider:
    """Local embeddings via ONNX Runtime. Runs anywhere ONNX Runtime does."""

    model: str = DEFAULT_MODEL
    dim: int = DEFAULT_DIM
    name: str = PROVIDER_NAME
    _session: Any = field(default=None, repr=False, compare=False)
    _tokenizer: Any = field(default=None, repr=False, compare=False)
    _inputs: frozenset = field(default=frozenset(), repr=False, compare=False)

    def available(self) -> str | None:
        """Import-level checks only. No network, no weight load, no I/O."""
        try:
            import numpy  # noqa: F401
            import onnxruntime  # noqa: F401
            import tokenizers  # noqa: F401
            from huggingface_hub import hf_hub_download  # noqa: F401
        except ImportError:
            return "onnxruntime is not installed (`uv sync --extra semantic`)"
        return None

    def _ensure_loaded(self):
        if self._session is not None:
            return self._session, self._tokenizer

        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        def fetch(filename: str) -> str:
            """Cache first, network only if it is genuinely missing.

            ``hf_hub_download`` otherwise revalidates against the Hub on every
            call, so a cached model still meant a network round trip (and an
            unauthenticated-request warning on stderr) every time the daemon
            loaded it. Once the weights are here they never change: the model
            id is pinned, and a different id is a different provider.
            """
            try:
                return hf_hub_download(self.model, filename, local_files_only=True)
            except Exception:
                return hf_hub_download(self.model, filename)

        # First call downloads if absent. Deliberately not in available().
        onnx_path = fetch(ONNX_FILE)
        tokenizer_path = fetch(TOKENIZER_FILE)

        tokenizer = Tokenizer.from_file(tokenizer_path)
        tokenizer.enable_truncation(max_length=MAX_TOKENS)
        tokenizer.enable_padding()

        options = ort.SessionOptions()
        # Half the cores, not all of them. This runs in the daemon's background
        # embedder, where finishing a corpus a little slower is worth far more
        # than making the machine unpleasant to use while it does.
        options.intra_op_num_threads = max(1, (os.cpu_count() or 2) // 2)
        session = ort.InferenceSession(
            onnx_path, options, providers=["CPUExecutionProvider"])

        self._session, self._tokenizer = session, tokenizer
        self._inputs = frozenset(i.name for i in session.get_inputs())
        return session, tokenizer

    def embed(self, texts: Sequence[str]) -> Any:
        """``(len(texts), dim)`` float32, L2-normalised.

        Normalised here so every consumer can treat cosine as a dot product;
        ``embed.cosine_topk`` relies on it and would otherwise pay a division
        per row per query.
        """
        reason = self.available()
        if reason is not None:
            from .embed import EmbeddingUnavailable

            raise EmbeddingUnavailable(reason)
        # Every model call routes through the chokepoint, embeddings included
        # (.valholl/articles/model-policy-chokepoint.md).
        policy.check(self.model, self.name)

        import numpy as np

        rows = list(texts)
        if not rows:
            return np.zeros((0, self.dim), dtype=np.float32)

        session, tokenizer = self._ensure_loaded()
        out: list[Any] = []
        for start in range(0, len(rows), BATCH):
            encoded = tokenizer.encode_batch(rows[start:start + BATCH])
            ids = np.array([e.ids for e in encoded], dtype=np.int64)
            feeds = {
                "input_ids": ids,
                "attention_mask": np.array(
                    [e.attention_mask for e in encoded], dtype=np.int64),
            }
            # This export declares token_type_ids; others do not. Feeding an
            # input the graph never declared is an error, so it is asked for.
            if "token_type_ids" in self._inputs:
                feeds["token_type_ids"] = np.zeros_like(ids)
            # BGE pools the CLS token, not the mean. Mean pooling here would
            # produce plausible vectors that quietly rank worse — the model was
            # trained with CLS and its own card specifies it.
            out.append(session.run(None, feeds)[0][:, 0])

        array = np.concatenate(out, axis=0).astype(np.float32)
        norms = np.linalg.norm(array, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        return (array / norms).astype(np.float32)
