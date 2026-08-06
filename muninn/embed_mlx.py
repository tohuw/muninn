"""The local, offline embedding provider. Behind the ``[semantic]`` extra.

Never imported at package import time — ``muninn.embed.resolve_provider``
imports it inside a function, and importing *this* module is what pulls MLX.
That indirection is the reason a default install can register the ``--semantic``
flag without owning a model runtime.

MLX is Apple-silicon only, which is a real limitation and not a temporary one:
this module reports it through ``available()`` rather than failing at import, so
a Linux user gets "not available here, install a plugin" instead of a traceback
from a package that had no business being loaded.

**No weights are downloaded by ``available()``.** It answers from imports alone.
The first ``embed()`` call is where a model is fetched, and that is the honest
place for it — a probe that quietly pulled several hundred megabytes during
plugin discovery would be a hang with no explanation.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import policy

# Set at *module* scope, before anything imports huggingface_hub, because that
# library reads this once at import and ignores later changes. Setting it inside
# the load function was too late: ``available()`` imports ``mlx_embeddings``
# first, which locks the setting in, and the "Fetching 10 files" bar printed on
# every single semantic search.
#
# Cosmetic rather than a correctness fix — the bar goes to stderr, so `--json`
# on stdout stays parseable, which was checked because that is the agent-facing
# contract and would have been a silent break. It is a progress bar for a fetch
# that is a no-op once the weights are cached.
#
# Safe here specifically because this module is imported lazily and only when
# MLX is about to be used; muninn's other modules never reach it.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

#: A small, fast, well-understood sentence embedding model. Named here rather
#: than configured because the model id is part of the *identity* of the vectors
#: it produces — it goes into the primary key, and two models' vectors are not
#: comparable — so changing it is a re-embed, not a setting.
DEFAULT_MODEL = "mlx-community/bge-small-en-v1.5-bf16"
DEFAULT_DIM = 384

PROVIDER_NAME = "mlx-local"


@dataclass
class MLXEmbeddingProvider:
    """Local embeddings via ``mlx-embeddings``. Apple silicon only."""

    model: str = DEFAULT_MODEL
    dim: int = DEFAULT_DIM
    name: str = PROVIDER_NAME
    _loaded: Any = field(default=None, repr=False, compare=False)

    def available(self) -> str | None:
        """Import-level checks only. No network, no weight load, no I/O."""
        if sys.platform != "darwin":
            return f"mlx is Apple-silicon only; this is {sys.platform}"
        try:
            import mlx.core  # noqa: F401
            import mlx_embeddings  # noqa: F401
        except ImportError:
            return "mlx-embeddings is not installed (`uv sync --extra semantic`)"
        return None

    def _ensure_loaded(self):
        if self._loaded is None:
            import mlx_embeddings

            # First call downloads weights if absent. Deliberately not in
            # available() — see the module docstring.
            self._loaded = mlx_embeddings.load(self.model)
        return self._loaded

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
        # Every model call routes through the chokepoint, embeddings included —
        # an embedding call is still a model call
        # (.valholl/articles/model-policy-chokepoint.md).
        policy.check(self.model, self.name)

        import mlx_embeddings
        import numpy as np

        model, tokenizer = self._ensure_loaded()
        output = mlx_embeddings.generate(model, tokenizer, list(texts))
        array = np.asarray(output.text_embeds, dtype=np.float32)
        norms = np.linalg.norm(array, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        return (array / norms).astype(np.float32)
