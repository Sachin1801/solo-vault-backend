from abc import ABC, abstractmethod

from FlagEmbedding import BGEM3FlagModel

from app.cache.hashing import chunk_hash
from app.cache.redis_cache import cache_embedding, get_cached_embedding
from app.config import settings
from app.types import ChunkResult, EmbedResult


class EmbeddingModel(ABC):
    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class LocalBgeM3Embedding(EmbeddingModel):
    def __init__(self):
        self.model = BGEM3FlagModel(settings.embedding_model, use_fp16=False)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        embeddings = self.model.encode(texts, return_dense=True)["dense_vecs"]
        vectors: list[list[float]] = []
        for emb in embeddings:
            vec = emb.tolist() if hasattr(emb, "tolist") else list(emb)
            if len(vec) > settings.embedding_dim:
                vec = vec[: settings.embedding_dim]
            elif len(vec) < settings.embedding_dim:
                vec = vec + [0.0] * (settings.embedding_dim - len(vec))
            vectors.append(vec)
        return vectors


_model: EmbeddingModel | None = None


def get_model() -> EmbeddingModel:
    global _model
    if _model is None:
        _model = LocalBgeM3Embedding()
    return _model


def embed(chunks: list[ChunkResult]) -> list[EmbedResult]:
    model = get_model()
    results: list[EmbedResult | None] = []
    cache_misses: list[tuple[int, ChunkResult]] = []

    for c in chunks:
        h = chunk_hash(c.content)
        cached = get_cached_embedding(h)
        if cached is not None:
            vec = cached[: settings.embedding_dim]
            if len(vec) < settings.embedding_dim:
                vec = vec + [0.0] * (settings.embedding_dim - len(vec))
            results.append(
                EmbedResult(
                    chunk_index=c.chunk_index,
                    content=c.content,
                    embedding=vec,
                    token_count=c.token_count,
                )
            )
        else:
            cache_misses.append((len(results), c))
            results.append(None)

    if cache_misses:
        texts = [c.content for _, c in cache_misses]
        vectors = model.embed_batch(texts)
        for (pos, c), vec in zip(cache_misses, vectors):
            h = chunk_hash(c.content)
            cache_embedding(h, vec)
            results[pos] = EmbedResult(
                chunk_index=c.chunk_index,
                content=c.content,
                embedding=vec,
                token_count=c.token_count,
            )

    return [r for r in results if r is not None]
