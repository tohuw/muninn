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

**Memory is a property of this module, not of the daemon that hosts it.**
:mod:`embedder` runs inside ``muninn serve``, a process that lives for weeks
and embeds for a few minutes of that, so anything a forward pass allocates and
does not return is a cost the user pays continuously for work that finished
days ago. Two defaults were doing precisely that until measured — ONNX
Runtime's arena allocator and a batch size of 32 — and together they held
2.1 GB resident in an idle daemon with an empty backlog. :data:`BATCH` and
``_ensure_loaded`` each carry the number that justifies the current value;
.valholl/articles/corpus-measurements.md, "Embedding memory", has the table.
The general lesson is the one worth carrying to the next provider written
here: a default tuned for a batch job is the wrong default inside a daemon,
and peak memory is the metric rather than steady-state memory, because an
arena makes them the same number.
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

#: BERT-family context limit. Truncation is here so a long chunk cannot raise
#: instead of embedding — but note that for this corpus it is the *ordinary*
#: path, not a guard against a pathological outlier: ``store.chunk_text``
#: targets 400 words, which measures at a median of 540 tokens, and 85% of
#: real chunks reach this cap (.valholl/articles/corpus-measurements.md,
#: "Embedding memory"). Every full-width chunk loses its tail. That is a
#: retrieval question, recorded where it can be weighed, and deliberately not
#: changed here: widening the window is a re-embed of the whole archive.
MAX_TOKENS = 512

#: Rows per forward pass. Padding is to the longest row *in the batch*, and
#: because of the measurement above essentially every batch pads to the full
#: 512 — so this constant alone sets the size of the attention tensors, which
#: are quadratic in sequence length: ``batch × 12 heads × 512 × 512 × 4 B`` is
#: 400 MB per score tensor at 32, with several live at once.
#:
#: 32 cost 2.1 GB of resident memory for the life of the process; 8 costs
#: ~230 MB — and ONNX Runtime's arena never returns the peak (see
#: ``_ensure_loaded``), so in the daemon the peak *is* the footprint. An
#: earlier comment here claimed 32 "bounds peak memory"; it bounded the
#: per-row padding cost and nothing else.
#:
#: The throughput this gives up is **nothing measurable**. A 64-row
#: microbenchmark said ~10% and that number is misleading: it is dominated by
#: the first passes, where the arena's allocation is still being paid for. Rerun
#: as a corpus pass — 3,395 real chunk bodies through the daemon's own 64-row
#: call path — it came to 137 s against the 138 s on record for 32-with-arena.
BATCH = 8

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
        # The arena is ONNX Runtime's default allocator and it never hands the
        # peak back to the OS. In a batch process that is free; in the daemon,
        # which lives for weeks and embeds for minutes of that, it means one
        # backlog drain sets the resident footprint permanently. Measured on a
        # 32-core Windows host: a single 64-row pass of cap-length chunks took
        # the process from 215 MB to 2,110 MB resident and it stayed there
        # across later passes, including passes of short rows. The arena is not
        # leaking — it is a high-water mark being honoured — which is exactly
        # why no amount of waiting or idling brought it back down.
        #
        # Turning it off holds the process at ~230 MB, and on a corpus-scale
        # pass it is not a trade at all: 3,395 real chunks took 137 s with the
        # arena off at BATCH=8 against the 138 s on record with it on at 32.
        # A 64-row microbenchmark does suggest ~10%, which is why the number
        # quoted here is the corpus one — the short run is measuring the
        # arena's own first allocation, not steady-state inference.
        options.enable_cpu_mem_arena = False
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
