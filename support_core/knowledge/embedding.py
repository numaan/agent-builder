"""Turning text into vectors. Implements the embedding half of DESIGN.md sections 9.1 and 9.3.

Two protocols, because dense retrieval and late interaction are different shapes and forcing one
into the other's signature would hide the difference that matters:

* :class:`Embedder` produces **one vector per chunk**. It is what ``doc_chunk.embedding`` holds
  and what pgvector's cosine distance scores. This is also the seam a hosted embedding API drops
  into - see :class:`CallableEmbedder`.
* :class:`LateInteractionEncoder` produces **one vector per token**. It is what Qdrant stores as
  a multivector and scores with MaxSim: for each query token, the best-matching document token,
  summed. That is the whole of ColBERT's advantage on this corpus shape - short policy passages
  where the answer turns on a phrase - and it is why the decisions log put it in this phase.

**Which encoder actually runs here.** :class:`DeterministicEncoder`, and that is a limitation
rather than a preference. This deployment has no embedding API (the GLM endpoint is
Anthropic-compatible and serves none), which is the argument that brought ColBERT forward into
phase 5: it runs locally. :class:`FastEmbedColbertEncoder` is that local model, and it is
written and unexercised, because this environment cannot complete TLS to the model host and so
cannot download the weights. The deterministic encoder is therefore what the tests measure and
what a demo runs, and reviews/phase-5.md says plainly what that does and does not prove.

**Why deterministic and not random.** The same reason phase 3's delimiter token is a hash rather
than ``secrets.token_hex``: the fake provider replays by request fingerprint, so a corpus that
embedded differently on each process would make every cassette a miss, and a re-sync of
unchanged content would produce a new index for no reason. Everything here is keyed on
``blake2b``, never on Python's ``hash``, which is salted per process and would silently give one
answer in the sync and another in the query.
"""

import math
import re
from collections.abc import Callable, Sequence
from hashlib import blake2b
from typing import Any, Protocol, runtime_checkable

DIMENSIONS = 128
"""The width of every vector this module produces, and of ``doc_chunk.embedding``.

128 because that is what ``colbert-ir/colbertv2.0`` emits per token. Pinning the dense side to
the same width means the two halves of the knowledge layer cannot disagree about a dimension,
which is exactly the failure phase-0 finding N12 described: a dimensionless column that accepted
both and failed at query time. Migration ``0010`` pins the column; a test asserts the two
constants agree.
"""

MAX_DOCUMENT_VECTORS = 256
"""Token vectors kept per chunk. A 500-token chunk of prose has fewer distinct tokens than that,
so the cap bites only on something pathological - a chunk of unique identifiers - where the tail
carries no meaning and would cost the Qdrant point its size for nothing."""

MAX_QUERY_VECTORS = 32
"""Token vectors kept per query. A customer question is a sentence."""

_TOKEN = re.compile(r"[0-9a-z]+(?:'[a-z]+)?")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens. Deliberately the same rule the lexical side would recognise."""
    return _TOKEN.findall(text.lower())


@runtime_checkable
class Embedder(Protocol):
    """One vector per text (DESIGN.md section 9.1's pgvector half)."""

    name: str
    dimensions: int

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


@runtime_checkable
class LateInteractionEncoder(Protocol):
    """One vector per token, scored by MaxSim (DESIGN.md section 9.1's ColBERT half)."""

    name: str
    dimensions: int

    async def encode_documents(self, texts: Sequence[str]) -> list[list[list[float]]]: ...

    async def encode_query(self, text: str) -> list[list[float]]: ...


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        # A token whose hash cancels to zero is possible in principle. A zero vector has no
        # direction, so cosine against it is undefined; one non-zero component is a direction
        # nothing else shares, which is the honest answer for a token that carries no signal.
        return [1.0] + [0.0] * (len(vector) - 1)
    return [value / norm for value in vector]


def _hash_vector(text: str) -> list[float]:
    """A fixed pseudo-random unit vector for one string, stable across processes and platforms.

    ``blake2b`` with an explicit digest size, expanded two bytes per dimension into the range
    [-1, 1). ``hash()`` would have been shorter and is unusable: it is salted per interpreter, so
    a corpus indexed by one process would not be findable by the next.
    """
    needed = DIMENSIONS * 2
    digest = b""
    counter = 0
    while len(digest) < needed:
        digest += blake2b(f"{counter}\x00{text}".encode(), digest_size=64).digest()
        counter += 1
    return [
        int.from_bytes(digest[index * 2 : index * 2 + 2], "big") / 32768.0 - 1.0
        for index in range(DIMENSIONS)
    ]


_TRIGRAM_WEIGHT = 0.35
"""How much a token's character trigrams contribute beside the token itself.

Without them every token is orthogonal to every other and "refund" misses "refunds" entirely,
which would make the deterministic encoder a worse lexical matcher than the tsvector it sits
beside. With them, morphological variants are close and unrelated words still are not. It is a
crude stand-in for a distributional model and is not pretending otherwise."""


def _token_vector(token: str) -> list[float]:
    padded = f"^{token}$"
    accumulated = _hash_vector(token)
    for index in range(len(padded) - 2):
        trigram = padded[index : index + 3]
        contribution = _hash_vector(f"#{trigram}")
        for position in range(DIMENSIONS):
            accumulated[position] += _TRIGRAM_WEIGHT * contribution[position]
    return _unit(accumulated)


class DeterministicEncoder:
    """A local encoder with no model, no download and no dependency.

    What it is: a hashed projection of tokens and their character trigrams onto the unit sphere.
    Cosine similarity between two chunk vectors is therefore close to a smoothed token overlap,
    and MaxSim over the token vectors is close to a per-query-token best match. Both behave the
    way their real counterparts behave *structurally* - which is what the retrieval code, the
    Qdrant multivector configuration and the merge need to be exercised against - and neither
    knows that "reimbursement" and "refund" mean the same thing, which is what a real model is
    for.

    It is the default because it is what this deployment can run (see the module docstring), and
    because a test suite that needed a 400 MB download would be a test suite nobody runs.
    """

    name = "deterministic-hash-128"
    dimensions = DIMENSIONS

    def _document(self, text: str) -> list[float]:
        tokens = tokenize(text)
        if not tokens:
            return _unit([0.0] * DIMENSIONS)
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        total = [0.0] * DIMENSIONS
        for token, count in counts.items():
            # sqrt term frequency: a word repeated forty times is more important than one used
            # once, and not forty times more.
            weight = math.sqrt(count)
            vector = _token_vector(token)
            for position in range(DIMENSIONS):
                total[position] += weight * vector[position]
        return _unit(total)

    def _tokens(self, text: str, limit: int) -> list[list[float]]:
        seen: dict[str, None] = {}
        for token in tokenize(text):
            if token not in seen:
                seen[token] = None
            if len(seen) >= limit:
                break
        if not seen:
            return [_unit([0.0] * DIMENSIONS)]
        return [_token_vector(token) for token in seen]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._document(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._document(text)

    async def encode_documents(self, texts: Sequence[str]) -> list[list[list[float]]]:
        return [self._tokens(text, MAX_DOCUMENT_VECTORS) for text in texts]

    async def encode_query(self, text: str) -> list[list[float]]:
        return self._tokens(text, MAX_QUERY_VECTORS)


class FastEmbedColbertEncoder:
    """The real local ColBERT, through ``fastembed``'s ONNX runtime.

    This is the encoder the decisions log means by "it runs locally, which removes this phase's
    worst limitation". It is behind an optional extra (``pip install support-core[colbert]``)
    and imported lazily, so neither CI nor a plain install pulls onnxruntime and neither
    downloads 400 MB of weights to run a test that does not need them.

    **It has never run in this environment.** ``fastembed`` installs; the model host cannot be
    reached from here (TLS fails with ``CERTIFICATE_VERIFY_FAILED``), so the weights cannot be
    fetched. That is recorded in reviews/phase-5.md exactly as phase 3 recorded the same shape
    of gap for ``AnthropicProvider``: the seam is real and the code is written, and nothing here
    should be read as evidence that it works.

    ``dimensions`` is declared rather than measured because it decides a database column, and a
    model whose width does not match is refused at construction rather than at the first insert.
    """

    name = "colbert-ir/colbertv2.0"
    dimensions = DIMENSIONS

    def __init__(self, model_name: str = "colbert-ir/colbertv2.0") -> None:
        try:
            from fastembed import LateInteractionTextEmbedding
        except ImportError as exc:  # pragma: no cover - the extra is not installed in CI
            msg = (
                "the ColBERT encoder needs the 'colbert' extra: pip install "
                "'support-core[colbert]'. Without it the knowledge layer runs on "
                "DeterministicEncoder, which is local but is not a language model."
            )
            raise RuntimeError(msg) from exc
        self.name = model_name
        self._model = LateInteractionTextEmbedding(model_name)

    async def encode_documents(self, texts: Sequence[str]) -> list[list[list[float]]]:
        return [self._check(vectors) for vectors in self._model.embed(list(texts))]

    async def encode_query(self, text: str) -> list[list[float]]:
        return self._check(next(iter(self._model.query_embed([text]))))

    def _check(self, vectors: Any) -> list[list[float]]:
        # ``Any`` because what fastembed yields is a numpy array whose stubs are not installed
        # here, and the next line is the boundary at which it becomes ordinary floats.
        rows = [[float(value) for value in row] for row in vectors]
        for row in rows:
            if len(row) != self.dimensions:
                msg = (
                    f"{self.name} produced {len(row)}-dimensional vectors and doc_chunk.embedding "
                    f"is vector({self.dimensions}); a different width needs its own migration"
                )
                raise ValueError(msg)
        return rows


EmbedCall = Callable[[Sequence[str]], list[list[float]]]


class CallableEmbedder:
    """A hosted embedding API, in the smallest shape that is still the real seam.

    There is no such API in this deployment, so nothing here calls one. What this class is for is
    that adopting one should be a few lines in the composition root rather than a change to the
    retriever: hand it the function that calls the vendor and its width, and
    :class:`~support_core.knowledge.document.DocumentRetriever` neither knows nor cares.

    The width is checked on every batch rather than trusted, because the failure it prevents -
    a provider quietly changing its default model - is one that would otherwise surface as
    pgvector refusing an insert halfway through a sync.
    """

    def __init__(self, call: EmbedCall, *, name: str, dimensions: int = DIMENSIONS) -> None:
        self.name = name
        self.dimensions = dimensions
        self._call = call

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._call(list(texts))
        if len(vectors) != len(texts):
            msg = f"{self.name} returned {len(vectors)} vectors for {len(texts)} texts"
            raise ValueError(msg)
        for vector in vectors:
            if len(vector) != self.dimensions:
                msg = (
                    f"{self.name} returned a {len(vector)}-dimensional vector and this index is "
                    f"vector({self.dimensions})"
                )
                raise ValueError(msg)
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]


def maxsim(query: Sequence[Sequence[float]], document: Sequence[Sequence[float]]) -> float:
    """ColBERT's late-interaction score: for each query token, its best document token, summed.

    Qdrant computes this natively, which is the reason the decisions log chose it over a PLAID
    index directory or MaxSim written out in SQL. This implementation exists so the tests can say
    what the right answer is without asking the thing under test, and so a corpus small enough to
    hold in memory can be scored when Qdrant is not there.
    """
    if not query or not document:
        return 0.0
    total = 0.0
    for q in query:
        total += max(sum(a * b for a, b in zip(q, d, strict=True)) for d in document)
    return total
